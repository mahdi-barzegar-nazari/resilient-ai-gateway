"""Central LLM router: Groq <-> Gemini with automatic, transparent failover.

Routing rules
-------------
* Images and PDFs always go to Gemini (the only multimodal provider; no failover path).
* The user's selected model decides which provider is tried first.
* If the first provider fails (HTTP 429, timeout, empty answer ...) the same request is
  replayed on the other provider and a short note is appended to the answer.
* In exam mode the note is suppressed so the machine-readable answer-sheet line survives.
* Live web context is injected only when the request is served by Gemini (the grounded engine).
* Several keys per provider are rotated *below* this layer (``services/clients.py`` and
  ``services/key_pool.py``): a provider call either succeeds with some key or raises, and only then does
  the cross-provider failover above apply. When every key of a provider is cooling down the call raises
  at once, so failover is just as immediate as with a single key.
"""
import logging
from datetime import datetime

import config
from core.prompts import (
    BASE_SYSTEM_PROMPT, EXAM_SYSTEM_PROMPT, LONG_SYSTEM_PROMPT,
    FAILOVER_NOTE_TO_GEMINI, FAILOVER_NOTE_TO_GROQ,
)
from services import clients
from services.media_service import PDF_MIME
from services.web_service import should_search_web, fetch_web_context
from utils.security import short_err

from google.genai import types

log = logging.getLogger(__name__)


def with_web_context(user_text, is_exam=False, use_web=True):
    """Append live web data to the prompt when the question needs it (use_web=False disables it)."""
    if use_web and not is_exam and should_search_web(user_text):
        web_info = fetch_web_context(user_text)
        if web_info:
            return f"{user_text}\n\n[داده‌های زنده وب]:\n{web_info}"
    return user_text


def call_gemini(user_text, sys_prompt, history, is_exam=False, is_long=False, image=None):
    """Call Gemini. ``image`` is ``{"data": bytes, "mime": str}`` (PDFs use the same shape).

    Raises on any error or empty answer so the router can fail over.
    """
    if not clients.gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    contents = []
    for item in history:
        role = "user" if item["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": item["content"]}]})

    parts = []
    if image:
        parts.append(types.Part.from_bytes(data=image["data"], mime_type=image["mime"]))
    parts.append(types.Part(text=user_text))
    contents.append(types.Content(role="user", parts=parts))

    # PDFs are heavy; they use the client with the longer timeout
    client = clients.gemini_pdf_client if (image and image.get("mime") == PDF_MIME) else clients.gemini_client
    resp = client.models.generate_content(
        model=config.MODEL_GEMINI,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=sys_prompt,
            temperature=0.1 if (is_exam or is_long) else 0.2
        )
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("پاسخ خالی از Gemini")
    return text


def call_groq(user_text, sys_prompt, model_name, history, is_exam=False, is_long=False):
    """Call Groq. Raises on any error or empty answer so the router can fail over."""
    if not clients.groq_client:
        raise RuntimeError("GROQ_API_KEY is not configured")

    if is_exam:
        max_tok = 300
    elif is_long:
        max_tok = config.LONG_MODE_MAX_TOKENS
    else:
        max_tok = 1500 if model_name == config.MODEL_SMART else 800

    messages = [{"role": "system", "content": sys_prompt}] + list(history) + [{"role": "user", "content": user_text}]
    resp = clients.groq_client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.1 if (is_exam or is_long) else 0.2,
        max_tokens=max_tok
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("پاسخ خالی از Groq")
    return text


def process_ai_query(user_text, model_name=None, use_memory=False, history_list=None,
                     is_exam=False, is_long=False, image=None, use_web=True, task_prompt=None):
    """Answer a prompt. Returns ``(answer, note)``.

    ``note`` is non-empty only when a failover happened (and never in exam mode).

    - use_memory/history_list: prior messages are sent only when memory is on and not in exam mode.
    - use_web=False: skip automatic web enrichment (used for link-summary prompts).
    - task_prompt: extra system instructions of a tool (/debug, /quiz ...).
    - image: image or PDF as ``{"data": bytes, "mime": str}``; always handled by Gemini.
    """
    model_name = model_name or config.MODEL_FAST
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    sys_prompt = BASE_SYSTEM_PROMPT
    if is_exam:
        sys_prompt += f"\n\n{EXAM_SYSTEM_PROMPT}"
    elif is_long:
        sys_prompt += f"\n{LONG_SYSTEM_PROMPT}"
    if task_prompt:
        sys_prompt += f"\n{task_prompt}"
    sys_prompt += f"\nزمان فعلی سرور: {current_time}"

    history = list(history_list) if (use_memory and history_list and not is_exam) else []

    if not clients.gemini_client and not clients.groq_client:
        return "سرویس‌های هوش مصنوعی پیکربندی نشده‌اند.", ""

    # Multimodal input: Gemini only
    if image is not None:
        if not clients.gemini_client:
            return "تحلیل تصویر به GEMINI_API_KEY نیاز دارد.", ""
        return call_gemini(user_text, sys_prompt, history, is_exam, is_long, image=image), ""

    groq_model = model_name if model_name != config.MODEL_GEMINI else config.MODEL_FAST
    gemini_first = bool(clients.gemini_client) and (model_name == config.MODEL_GEMINI or not clients.groq_client)

    if gemini_first:
        enriched = with_web_context(user_text, is_exam, use_web)
        try:
            return call_gemini(enriched, sys_prompt, history, is_exam, is_long), ""
        except Exception as e1:
            log.warning("[failover] Gemini failed: %s", short_err(e1))
            if not clients.groq_client:
                raise
            try:
                text = call_groq(enriched, sys_prompt, groq_model, history, is_exam, is_long)
            except Exception as e2:
                raise RuntimeError(f"Gemini ({short_err(e1)}) | Groq ({short_err(e2)})")
            return text, ("" if is_exam else FAILOVER_NOTE_TO_GROQ)

    try:
        return call_groq(user_text, sys_prompt, groq_model, history, is_exam, is_long), ""
    except Exception as e1:
        log.warning("[failover] Groq failed: %s", short_err(e1))
        if not clients.gemini_client:
            raise
        enriched = with_web_context(user_text, is_exam, use_web)
        try:
            text = call_gemini(enriched, sys_prompt, history, is_exam, is_long)
        except Exception as e2:
            raise RuntimeError(f"Groq ({short_err(e1)}) | Gemini ({short_err(e2)})")
        return text, ("" if is_exam else FAILOVER_NOTE_TO_GEMINI)


def quick_web_answer(query):
    """/web: always fetch live context and answer with Gemini (the web-grounded engine)."""
    web_data = fetch_web_context(query)
    prompt = f"پرسش کاربر: {query}\n\n[داده‌های زنده وب]:\n{web_data}" if web_data else query
    return call_gemini(prompt, BASE_SYSTEM_PROMPT, [])
