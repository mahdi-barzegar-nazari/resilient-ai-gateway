"""Provider SDK clients, created once at import time from the configured keys.

A client is ``None`` when its API key is missing; callers must check before use.
Other modules access them as ``clients.groq_client`` (module attribute lookup at call
time) so tests can replace them.

Several keys per provider
-------------------------
With exactly one key the plain SDK client is returned, built exactly as before. With two or more keys
the client is a small facade exposing only the calls this project makes (``chat.completions.create``,
``audio.transcriptions.create``, ``models.generate_content``); each call runs through a
``services.key_pool.KeyPool``, which rotates to the next key on 429/quota, timeout, connection error,
5xx or an empty answer. Every key gets its own SDK client with **SDK-level retries disabled**, so the
SDK's back-off cannot multiply the bound set by ``config.KEY_MAX_ATTEMPTS``. Both Gemini clients (normal
and long-timeout PDF) share one pool, so a cooldown learned through one applies to the other.
"""
from types import SimpleNamespace

import config
from groq import Groq
from google import genai
from google.genai import types
from services.key_pool import EMPTY_ANSWER, KeyPool


def _make_groq(key, max_retries):
    return Groq(api_key=key, max_retries=max_retries, timeout=60.0)


def _make_gemini(key, timeout_ms):
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))


def _empty_groq_answer(resp):
    try:
        text = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return EMPTY_ANSWER
    return None if (text or "").strip() else EMPTY_ANSWER


def _empty_gemini_answer(resp):
    return None if (getattr(resp, "text", None) or "").strip() else EMPTY_ANSWER


def _rotating(pool, by_key, invoke, check=None):
    """Build ``f(*args, **kwargs)`` that runs ``invoke(client_of_the_next_key, *args, **kwargs)``."""
    def run(*args, **kwargs):
        return pool.call(lambda key: invoke(by_key[key], *args, **kwargs), check)
    return run


def build_groq_client(keys, failover_available, make=_make_groq):
    """One key: the plain SDK client (SDK retries off only when Gemini can take over, as before).

    Several keys: a rotating facade whose per-key clients never retry on their own.
    """
    if len(keys) == 1:
        return make(keys[0], 0 if failover_available else 2)
    pool = KeyPool("groq", lambda: keys)
    by_key = {key: make(key, 0) for key in keys}  # key rotation replaces the SDK's own retries
    chat = _rotating(pool, by_key, lambda c, *a, **kw: c.chat.completions.create(*a, **kw), _empty_groq_answer)
    # An empty transcript is a legitimate answer for silence, so Whisper has no empty-answer check.
    audio = _rotating(pool, by_key, lambda c, *a, **kw: c.audio.transcriptions.create(*a, **kw))
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat)),
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=audio)),
    )


def build_gemini_client(keys, timeout_ms, pool=None, make=_make_gemini):
    """One key: the plain SDK client. Several keys: a rotating facade (pass a shared ``pool``)."""
    if len(keys) == 1:
        return make(keys[0], timeout_ms)
    pool = pool or KeyPool("gemini", lambda: keys)
    by_key = {key: make(key, timeout_ms) for key in keys}
    generate = _rotating(pool, by_key, lambda c, *a, **kw: c.models.generate_content(*a, **kw), _empty_gemini_answer)
    return SimpleNamespace(models=SimpleNamespace(generate_content=generate))


_groq_keys = config.api_keys("groq")
_gemini_keys = config.api_keys("gemini")
_gemini_pool = KeyPool("gemini", lambda: _gemini_keys)

# With a Gemini fallback available, Groq must fail fast (no SDK-level retries) so the
# failover happens immediately instead of after several backoff rounds.
groq_client = build_groq_client(_groq_keys, failover_available=bool(_gemini_keys)) if _groq_keys else None

gemini_client = build_gemini_client(_gemini_keys, 60000, _gemini_pool) if _gemini_keys else None

# Large PDFs can take well over 60 s to process, so they get a dedicated long-timeout client.
gemini_pdf_client = build_gemini_client(_gemini_keys, 180000, _gemini_pool) if _gemini_keys else None
