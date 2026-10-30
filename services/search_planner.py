"""Search planner: one tiny LLM call that decides whether a message needs fresh web data and writes the query.

A keyword list cannot tell "قیمت دلار" from "قیمت‌گذاری در اقتصاد خرد را توضیح بده", and a follow-up such as "و یورو؟" contains no
keyword at all. The planner reads the message plus the last few messages of history and answers with strict JSON
``{"search": bool, "query": str}``.

It is an optimisation, never a dependency: :func:`plan_search` always returns a plan and never raises.

* Greetings, very short messages and explicit search phrases are decided without any call
  (``web_service.obvious_search_decision``).
* Otherwise one call goes to the fast Groq model (Gemini when Groq is missing or its call fails): temperature 0, a short
  timeout (``config.SEARCH_PLANNER_TIMEOUT_SECONDS``), a tiny prompt.
* A timeout, an error, an empty answer, invalid JSON or ``SEARCH_PLANNER_ENABLED=false`` falls back to the old keyword heuristic.

The calls use the normal provider clients, so API-key rotation applies to them. Nothing here touches the per-user rate limiter:
the planner belongs to the user request that triggered it and is not counted as another one. Logs never contain message text.
"""
import json
import logging
import re
from collections import namedtuple

import config
from services import clients
from services import web_service as web
from utils.security import short_err

from google.genai import types

log = logging.getLogger(__name__)

# ``how`` says where the decision came from: "obvious" (skip rules), "planner" (the model) or "keywords" (the fallback).
SearchPlan = namedtuple("SearchPlan", "search query how")
OBVIOUS, PLANNER, KEYWORDS = "obvious", "planner", "keywords"

PLANNER_MAX_TOKENS = 256        # the answer is one short JSON object; the rest is headroom for a model that "thinks" first
HISTORY_MESSAGE_CHARS = 300     # every history message is clipped, so the prompt stays tiny
LATEST_MESSAGE_CHARS = 600

PLANNER_SYSTEM_PROMPT = """You are the search planner of a chat assistant. Decide whether the LATEST user message needs fresh \
information from a web search and, if so, write the search query.

search=true: prices and exchange rates, news and current events, sports results, weather, schedules, recent releases or \
versions, who currently holds a role, facts that may have changed, or an explicit request to look something up.
search=false: greetings and chit-chat, writing, rewriting or translating text, coding, math, explaining stable concepts, \
advice and opinions, and anything the recent messages already answer.

Use the recent messages only to understand the latest one: a follow-up like "and the euro?" must become a complete, \
self-contained query. The query has 2-8 keywords in the language that will search best (usually the user's), without filler \
words such as "please search".

Reply with ONE JSON object and nothing else: {"search": true, "query": "..."} or {"search": false, "query": ""}"""


# ---------------------------------------------------------------- prompt
def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def build_planner_input(text, history=None):
    """The user part of the planner call: the last few history messages (clipped) and the latest message."""
    count = max(0, int(config.SEARCH_PLANNER_HISTORY_MESSAGES))
    usable = [
        item for item in (history or [])
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and item.get("content")
    ]
    recent = usable[-count:] if count else []
    parts = []
    if recent:
        lines = "\n".join(f"{item['role']}: {_clip(item['content'], HISTORY_MESSAGE_CHARS)}" for item in recent)
        parts.append(f"Recent messages:\n{lines}")
    parts.append(f"Latest message:\n{_clip(text, LATEST_MESSAGE_CHARS)}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- answer parsing
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"```[A-Za-z0-9_+-]*")
MAX_PLAN_QUERY_CHARS = 200


def _json_candidates(text):
    """JSON values in ``text``: the whole text first, then whatever parses from each of the first few ``{``."""
    try:
        yield json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, match in enumerate(re.finditer(r"\{", text)):
        if index >= 5:
            break
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        yield value


def _valid_plan(value):
    """``(search, query)`` for a well-formed plan object, otherwise ``None``."""
    if not isinstance(value, dict):
        return None
    search, query = value.get("search"), value.get("query")
    if not isinstance(search, bool):  # 1, "true" and null are not booleans
        return None
    if not search:
        return False, ""
    if not isinstance(query, str):
        return None
    query = web.limit_query(query).strip(" \t\"'`")[:MAX_PLAN_QUERY_CHARS].strip()
    return (True, query) if query else None


def parse_plan(raw):
    """Parse the planner's answer into ``(search, query)``; ``None`` when it is unusable.

    Defensive on purpose: ``<think>`` blocks and Markdown code fences are stripped and the first valid JSON object is used even
    when prose surrounds it. Types are checked: ``search`` must be a real boolean and, when it is true, ``query`` a non-empty
    string. A false ``search`` needs nothing else.
    """
    if not isinstance(raw, str):
        return None
    text = _CODE_FENCE.sub("", _THINK_BLOCK.sub("", raw)).strip()
    if not text:
        return None
    try:
        for value in _json_candidates(text):
            plan = _valid_plan(value)
            if plan is not None:
                return plan
    except RecursionError:  # absurdly nested garbage
        return None
    return None


# ---------------------------------------------------------------- provider calls
def _ask_groq(system, user, timeout):
    resp = clients.groq_client.chat.completions.create(
        model=config.MODEL_FAST,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        max_tokens=PLANNER_MAX_TOKENS,
        timeout=timeout,
    )
    return (resp.choices[0].message.content or "").strip()


def _ask_gemini(system, user, timeout):
    resp = clients.gemini_client.models.generate_content(
        model=config.MODEL_GEMINI,
        contents=[types.Content(role="user", parts=[types.Part(text=user)])],
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            http_options=types.HttpOptions(timeout=int(timeout * 1000)),
        ),
    )
    return (resp.text or "").strip()


def ask_planner(text, history=None):
    """One planner call: Groq first, Gemini when Groq is missing or fails. ``(search, query)`` or ``None``.

    Only a failed call (error, timeout, empty answer) moves on to the next provider; an answer that is not valid JSON goes
    straight to the keyword fallback, so the normal case stays a single call.
    """
    system, user = PLANNER_SYSTEM_PROMPT, build_planner_input(text, history)
    timeout = config.SEARCH_PLANNER_TIMEOUT_SECONDS
    providers = []
    if clients.groq_client:
        providers.append(("Groq", _ask_groq))
    if clients.gemini_client:
        providers.append(("Gemini", _ask_gemini))
    for name, ask in providers:
        try:
            raw = ask(system, user, timeout)
            if not raw:
                raise RuntimeError("empty planner answer")
        except Exception as e:
            log.warning("[search-planner] %s call failed: %s", name, short_err(e))
            continue
        plan = parse_plan(raw)
        if plan is None:
            log.warning("[search-planner] %s answered with invalid JSON", name)
        return plan
    return None


# ---------------------------------------------------------------- the decision
def plan_search(text, history=None):
    """Decide whether ``text`` needs a web search and with which query -> :class:`SearchPlan`. Never raises."""
    try:
        obvious = web.obvious_search_decision(text)
        if obvious is not None:
            return SearchPlan(obvious, web.heuristic_query(text, history) if obvious else "", OBVIOUS)
        if config.SEARCH_PLANNER_ENABLED:
            plan = ask_planner(text, history)
            if plan is not None:
                return SearchPlan(plan[0], plan[1], PLANNER)
    except Exception as e:  # the planner must never be the reason an answer is missing
        log.warning("[search-planner] failed, using the keyword heuristic: %s", short_err(e))
    return _keyword_plan(text, history)


def _keyword_plan(text, history):
    try:
        search = web.should_search_web(text)
        return SearchPlan(search, web.heuristic_query(text, history) if search else "", KEYWORDS)
    except Exception as e:
        log.warning("[search-planner] keyword heuristic failed: %s", short_err(e))
        return SearchPlan(False, "", KEYWORDS)
