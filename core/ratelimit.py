"""Per-user rate limit for requests that can spend provider quota (Groq, Gemini, Tavily).

Both platform adapters call :func:`check_rate_limit` and only react to the returned
:class:`Decision`; everything else lives here, so Telegram and Bale cannot drift apart.

Algorithm: a sliding-window log. For every ``(platform, user_id)`` the limiter remembers the times of
its accepted requests; a new request is accepted while fewer than ``config.RATE_LIMIT_MAX`` of them are
younger than ``config.RATE_LIMIT_WINDOW_SECONDS``. Rejected requests are not recorded, so spamming never
extends a block, and a user's entry holds at most ``RATE_LIMIT_MAX`` timestamps.

A rejected request asks for a notice at most once per window per user (``Decision.notify``); the rest
are dropped silently. Entries nobody needs any more are swept out (at most once per window, from inside
``check``), so memory follows the number of users active in the last window, not the number ever seen.

Everything is in memory and guarded by one lock that is never held during I/O. The clock is injectable
(``RateLimiter(clock=...)``), so tests never sleep. Settings are read as ``config.NAME`` at call time.
"""
import math
import threading
import time
from collections import deque
from dataclasses import dataclass

import config

# Tools that never reach Groq, Gemini or Tavily: they only call the free Wikipedia / ArXiv APIs, so they
# do not spend the quota this limit protects. Every other tool counts, which means a tool added later is
# limited until someone lists it here on purpose.
QUOTA_FREE_TOOLS = frozenset({"wiki", "arxiv"})


def is_metered_tool(cmd):
    """True when running the tool command ``cmd`` counts against the user's limit."""
    return cmd not in QUOTA_FREE_TOOLS


@dataclass(frozen=True)
class Decision:
    """Outcome of one check.

    ``allowed``      the request may proceed (and was counted).
    ``notify``       only when not allowed: send the "wait N seconds" notice now. False means drop silently.
    ``retry_after``  only when not allowed: whole seconds until a request would be accepted again (>= 1).
    """
    allowed: bool
    notify: bool = False
    retry_after: int = 0


ALLOWED = Decision(allowed=True)


class _Entry:
    __slots__ = ("hits", "notified_at")

    def __init__(self):
        self.hits = deque()        # times of accepted requests, oldest first
        self.notified_at = None    # when the last notice was requested


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}
        self._last_sweep = None

    # ------------------------------------------------------------ public API
    def check(self, platform, user_id):
        """Count one request of ``(platform, user_id)`` and decide whether it may proceed.

        The check and the counting are one atomic step, so concurrent requests of the same user can
        never both take the last free slot. Does nothing (and stores nothing) when the limit is disabled.
        """
        limit = config.RATE_LIMIT_MAX
        if limit <= 0:
            return ALLOWED
        window = config.RATE_LIMIT_WINDOW_SECONDS
        key = (platform, user_id)
        with self._lock:
            now = self._clock()
            self._sweep_if_due(now, window)
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _Entry()
            self._expire_hits(entry, now, window)
            if len(entry.hits) < limit:
                entry.hits.append(now)
                return ALLOWED
            # The slot that frees up first is the one that brings the count back below the limit.
            blocking_hit = entry.hits[len(entry.hits) - limit]
            retry_after = max(1, math.ceil(blocking_hit + window - now))
            notify = self._notice_due(entry, now, window)
            if notify:
                entry.notified_at = now
            return Decision(allowed=False, notify=notify, retry_after=retry_after)

    def sweep(self):
        """Drop entries that no longer matter and return how many were removed."""
        with self._lock:
            return self._sweep(self._clock(), config.RATE_LIMIT_WINDOW_SECONDS)

    def reset(self):
        """Forget every user (tests, or an operator hook)."""
        with self._lock:
            self._entries.clear()
            self._last_sweep = None

    def __len__(self):
        """Number of users currently tracked."""
        with self._lock:
            return len(self._entries)

    # ------------------------------------------------------------ internals (lock held)
    @staticmethod
    def _expire_hits(entry, now, window):
        hits = entry.hits
        while hits and now - hits[0] >= window:
            hits.popleft()

    @staticmethod
    def _notice_due(entry, now, window):
        return entry.notified_at is None or now - entry.notified_at >= window

    def _sweep_if_due(self, now, window):
        if self._last_sweep is None:
            self._last_sweep = now
        elif now - self._last_sweep >= window:
            self._sweep(now, window)

    def _sweep(self, now, window):
        # An entry is stale once all its hits expired AND its notice cooldown is over. Dropping it earlier
        # would forget that a notice was just sent, and the user could get a second one within one window.
        stale = []
        for key, entry in self._entries.items():
            self._expire_hits(entry, now, window)
            if not entry.hits and self._notice_due(entry, now, window):
                stale.append(key)
        for key in stale:
            del self._entries[key]
        self._last_sweep = now
        return len(stale)


limiter = RateLimiter()


def check_rate_limit(platform, user_id):
    """Count one AI-consuming request of a user. See :class:`Decision`."""
    return limiter.check(platform, user_id)
