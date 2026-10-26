"""Round-robin rotation over several API keys of one provider, with per-key cooldowns.

How a request is served (only when 2+ keys are configured)
-----------------------------------------------------------
1. The next key in round-robin order that is not cooling down is used.
2. A *rotatable* failure puts that key on cooldown and the **same request** is retried with the next
   key: HTTP 429 / quota (``Retry-After`` honoured, capped), timeout, connection error, 5xx, or an
   empty answer.
3. 401 / 403 gives the key a long cooldown, then rotates as well.
4. Anything else (400-class request errors, bugs) is re-raised at once: another key cannot fix it.
5. A key is tried at most once per request, at most ``config.KEY_MAX_ATTEMPTS`` keys are tried, and no
   new attempt starts once ``config.KEY_RETRY_BUDGET_SECONDS`` have passed. SDK-level retries are
   switched off by ``services/clients.py`` when rotating, so they cannot multiply this bound.
6. If every key is cooling down the call fails immediately (``AllKeysCoolingDown``, no sleeping), so
   the Groq <-> Gemini failover in ``ai_router`` takes over exactly as it does for any other error.

With a single key the pool is a plain pass-through: nothing is rotated, cooled down or logged, which
keeps single-key deployments identical to the behaviour before rotation existed.

State is in memory and guarded by a lock; the provider call itself runs outside the lock. Keys are
identified in logs and errors only by provider name and position, never by any part of the key.
Settings are read as ``config.NAME`` at call time, and the clock is injectable.
"""
import logging
import math
import threading
import time
from collections import namedtuple

import config

log = logging.getLogger(__name__)

# Failure kinds (also the human-readable reason in the rotation log line)
RATE_LIMITED = "rate limited"
AUTH_FAILED = "auth failed"
TRANSIENT = "transient error"
EMPTY = "empty answer"

Failure = namedtuple("Failure", "kind retry_after", defaults=(None,))
EMPTY_ANSWER = Failure(EMPTY)

# Time source for every pool without its own clock; tests replace it so they never sleep.
clock = time.monotonic

# Class-name fragments that identify network failures without importing any SDK
# (httpx, requests, groq and google-genai all name their timeout/connection errors this way).
_NETWORK_ERROR_HINTS = ("timeout", "connect", "networkerror", "remoteprotocol")


class AllKeysCoolingDown(RuntimeError):
    """Raised before any network call when every key of a provider is cooling down."""


# ---------------------------------------------------------------- classification
def retry_after_seconds(headers):
    """Seconds from a ``Retry-After`` header (any capitalisation), or ``None`` if absent or unusable."""
    try:
        items = list((headers or {}).items())
    except (AttributeError, TypeError):
        return None  # classification runs inside an except block: it must never raise itself
    for name, value in items:
        if str(name).lower() == "retry-after":
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return None  # an HTTP-date form is ignored: the default cooldown applies
            return seconds if math.isfinite(seconds) and seconds >= 0 else None
    return None


def classify_status(status, retry_after=None):
    """Failure for an HTTP status, or ``None`` when the status is not a reason to rotate."""
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    if status in (401, 403):
        return Failure(AUTH_FAILED)
    if status == 429:
        return Failure(RATE_LIMITED, retry_after)
    if status == 408 or 500 <= status <= 599:
        return Failure(TRANSIENT)
    return None


def _http_status(exc):
    """HTTP status carried by an SDK / requests exception (``status_code``, ``code`` or ``response``)."""
    for holder in (exc, getattr(exc, "response", None)):
        for attr in ("status_code", "code", "status"):
            value = getattr(holder, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
    return None


def _headers_of(exc):
    for holder in (getattr(exc, "response", None), exc):
        headers = getattr(holder, "headers", None)
        if headers:
            return headers
    return None


def _is_network_error(exc):
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    names = (cls.__name__.lower() for cls in type(exc).__mro__)
    return any(hint in name for name in names for hint in _NETWORK_ERROR_HINTS)


def _is_invalid_key_message(exc):
    # Gemini reports a bad key as HTTP 400 (API_KEY_INVALID). That is a problem with the key, not
    # with the request, so it is treated like a 401.
    text = str(exc).lower()
    return "api_key_invalid" in text or "api key not valid" in text


def classify_exception(exc):
    """Failure for an exception raised by a provider call; ``None`` means "do not rotate, re-raise"."""
    status = _http_status(exc)
    if status is not None:
        if status == 400 and _is_invalid_key_message(exc):
            return Failure(AUTH_FAILED)
        return classify_status(status, retry_after_seconds(_headers_of(exc)))
    if _is_network_error(exc):
        return Failure(TRANSIENT)
    return None


def cooldown_seconds(failure):
    """How long the key that produced ``failure`` is skipped. An empty answer is about the prompt, not the key."""
    if failure.kind == RATE_LIMITED:
        wait = config.KEY_COOLDOWN_RATE_LIMIT if failure.retry_after is None else failure.retry_after
        return max(0.0, min(wait, config.KEY_COOLDOWN_RATE_LIMIT_MAX))
    if failure.kind == AUTH_FAILED:
        return config.KEY_COOLDOWN_AUTH
    if failure.kind == TRANSIENT:
        return config.KEY_COOLDOWN_TRANSIENT
    return 0.0


# ---------------------------------------------------------------- the pool
class KeyPool:
    """Rotation state of one provider.

    ``keys`` is a callable returning the current key list (a lambda over a fixed list for the SDK
    clients, ``config.api_keys("tavily")`` for Tavily). ``clock`` defaults to the module-level ``clock``.
    """

    def __init__(self, name, keys, clock=None):
        self.name = name
        self._keys = keys
        self._clock = clock
        self._lock = threading.Lock()
        self._cooling_until = {}  # key -> clock value at which it may be used again
        self._next = 0            # round-robin cursor: index to start looking from

    def __repr__(self):  # never include keys
        return f"<KeyPool {self.name}>"

    def _now(self):
        return (self._clock or clock)()

    # -- internals -----------------------------------------------------------------
    def _acquire(self, keys, tried):
        """Atomically pick the next usable key (round-robin) and advance the cursor, or return ``None``."""
        now = self._now()
        with self._lock:
            start = self._next % len(keys)
            for step in range(len(keys)):
                index = (start + step) % len(keys)
                key = keys[index]
                if key in tried or self._cooling_until.get(key, 0.0) > now:
                    continue
                self._next = (index + 1) % len(keys)
                return index, key
        return None

    def _penalize(self, key, failure):
        seconds = cooldown_seconds(failure)
        if seconds > 0:
            with self._lock:
                until = self._now() + seconds
                if until > self._cooling_until.get(key, 0.0):  # never shorten a longer (e.g. auth) cooldown
                    self._cooling_until[key] = until
        return seconds

    def _all_cooling_down(self, keys):
        now = self._now()
        with self._lock:
            wait = min(self._cooling_until.get(key, 0.0) for key in keys) - now
        return AllKeysCoolingDown(
            f"all {len(keys)} {self.name} API keys are cooling down (next retry in {max(0.0, wait):.0f}s)"
        )

    # -- public --------------------------------------------------------------------
    def call(self, fn, check=None):
        """Run ``fn(key)`` with rotation and return its result.

        ``check(result)`` may return a ``Failure`` for a result that should be retried on another key
        (an empty answer, an HTTP 429 response...). If the attempts run out on such a result, that last
        result is returned unchanged so the caller's own error handling stays exactly as it was; if they
        run out on an exception, that exception is raised.
        """
        keys = list(self._keys())
        if not keys:
            raise RuntimeError(f"no {self.name} API key configured")
        if len(keys) == 1:
            return fn(keys[0])  # nothing to rotate to: behave exactly like a plain call

        started = self._now()
        tried = set()
        previous = None  # (index, failure, cooldown) of the attempt that just failed
        outcome = None   # ("raise", exc) or ("return", result) of the last failed attempt
        for attempt in range(max(1, int(config.KEY_MAX_ATTEMPTS))):
            if attempt and self._now() - started >= config.KEY_RETRY_BUDGET_SECONDS:
                break
            picked = self._acquire(keys, tried)
            if picked is None:
                if attempt == 0:
                    raise self._all_cooling_down(keys)
                break  # the remaining keys are cooling down or already tried
            index, key = picked
            if previous:
                log.info(
                    "[key-rotation] %s key #%d/%d %s (cooldown %.0fs); rotating to key #%d/%d",
                    self.name, previous[0] + 1, len(keys), previous[1].kind, previous[2], index + 1, len(keys),
                )
            tried.add(key)
            try:
                result = fn(key)
            except Exception as exc:
                failure = classify_exception(exc)
                if failure is None:
                    raise  # a request error (400...) or a bug: another key cannot help
                outcome = ("raise", exc)
            else:
                failure = check(result) if check else None
                if failure is None:
                    return result
                outcome = ("return", result)
            previous = (index, failure, self._penalize(key, failure))

        kind, value = outcome
        if kind == "raise":
            raise value
        return value
