"""Per-chat session state shared by every platform adapter.

A session is addressed by ``(platform, chat_id)``: the same numeric chat id on Telegram
and on Bale never collides. State lives in memory only, so it is reset on restart.
"""
import threading
from dataclasses import dataclass, field
from typing import List, Optional

import config

PLATFORM_TELEGRAM = "tg"
PLATFORM_BALE = "bale"

SETTING_ACTIONS = (
    "fast", "smart", "gemini",
    "memory_on", "memory_off",
    "exam_on", "exam_off",
    "long_on", "long_off",
)


@dataclass
class Session:
    model: str = field(default_factory=lambda: config.MODEL_FAST)
    memory_on: bool = False          # privacy-first default: memory is OFF until /memory_on
    exam_mode: bool = False
    long_mode: bool = False
    history: List[dict] = field(default_factory=list)
    quiz: Optional[dict] = None          # the question currently waiting for an answer
    quiz_stats: Optional[dict] = None    # score, topic, recent questions
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def apply_action(self, action):
        """Apply one of SETTING_ACTIONS. Returns False for an unknown action."""
        if action == "fast":
            self.model = config.MODEL_FAST
        elif action == "smart":
            self.model = config.MODEL_SMART
        elif action == "gemini":
            self.model = config.MODEL_GEMINI
        elif action == "memory_on":
            self.memory_on = True
        elif action == "memory_off":
            self.memory_on = False
            self.history = []
        elif action == "exam_on":
            self.exam_mode = True
        elif action == "exam_off":
            self.exam_mode = False
        elif action == "long_on":
            self.long_mode = True
        elif action == "long_off":
            self.long_mode = False
        else:
            return False
        return True

    def remember(self, user_text, answer):
        """Append one exchange and keep only the most recent messages."""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": answer})
        if len(self.history) > config.MAX_HISTORY_MESSAGES:
            self.history = self.history[-config.MAX_HISTORY_MESSAGES:]

    def clear_history(self):
        self.history = []


class SessionStore:
    """Thread-safe lazy registry of sessions."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get(self, platform, chat_id):
        key = (platform, chat_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = self._sessions[key] = Session()
            return session


sessions = SessionStore()
