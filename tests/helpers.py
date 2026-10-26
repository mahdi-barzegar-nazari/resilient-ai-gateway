"""Shared test utilities."""
import unittest
from types import SimpleNamespace
from unittest import mock

import config
from core.session import sessions
from telebot.apihelper import ApiTelegramException


class FakeResponse:
    """Minimal ``requests.Response`` look-alike."""

    def __init__(self, data=None, status=200, content=b"", text=""):
        self._data, self.status_code, self.content, self.text = data, status, content, text

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def make_api_error(description="Bad Request: can't parse entities"):
    return ApiTelegramException("sendMessage", None, {"description": description, "error_code": 400})


def tg_message(chat=1, user=111, text="hi", **extra):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat), from_user=SimpleNamespace(id=user), text=text, message_id=7, **extra
    )


def tg_call(data, chat=1, user=111, message_id=55):
    return SimpleNamespace(
        id="cb-1", data=data, from_user=SimpleNamespace(id=user),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=message_id),
    )


class BaseTestCase(unittest.TestCase):
    """Isolates every test: empty session store, open allow-list, no real sleeping."""

    def setUp(self):
        sessions._sessions.clear()
        patches = [
            mock.patch.object(config, "ALLOWED_USERS", []),
            mock.patch("time.sleep", lambda *_: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class Recorder:
    """Records calls made to patched methods and lets tests inspect them."""

    def __init__(self):
        self.calls = []

    def make(self, name, result=None):
        def function(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result
        return function

    def of(self, name):
        return [c for c in self.calls if c[0] == name]

    def texts(self, name="reply_to"):
        """Texts of reply_to/send_message calls (the text is the 2nd/2nd positional argument)."""
        return [c[1][1] for c in self.of(name)]


TELEGRAM_BOT_METHODS = (
    "reply_to", "send_message", "send_chat_action", "answer_callback_query",
    "edit_message_text", "edit_message_reply_markup", "get_file", "download_file",
)


def patch_bot(testcase, app, **results):
    """Replace every network-facing method of ``app.bot`` with a recorder; returns the Recorder."""
    recorder = Recorder()
    for name in TELEGRAM_BOT_METHODS:
        p = mock.patch.object(app.bot, name, recorder.make(name, results.get(name)))
        p.start()
        testcase.addCleanup(p.stop)
    return recorder


# ---------------------------------------------------------------- API key rotation helpers
class FakeClock:
    """Injectable clock: time only moves when a test calls ``advance``, so no test ever sleeps."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeHttpError(Exception):
    """Shaped like ``groq.APIStatusError`` / ``requests.HTTPError``: ``status_code`` and ``response.headers``."""

    def __init__(self, status, headers=None, message=None):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class FakeGenaiError(Exception):
    """Shaped like ``google.genai.errors.APIError``: ``code``, ``status`` (a string) and ``message``."""

    def __init__(self, code, status, message=""):
        super().__init__(f"{code} {status}. {message}")
        self.code, self.status, self.message = code, status, message


# Same class names as the real SDK errors: the pool recognises network failures by name, not by import.
class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


ROTATION_DEFAULTS = {
    "KEY_MAX_ATTEMPTS": 3,
    "KEY_RETRY_BUDGET_SECONDS": 90.0,
    "KEY_COOLDOWN_RATE_LIMIT": 60.0,
    "KEY_COOLDOWN_RATE_LIMIT_MAX": 300.0,
    "KEY_COOLDOWN_TRANSIENT": 15.0,
    "KEY_COOLDOWN_AUTH": 3600.0,
}


def pin_rotation_settings(testcase):
    """Fix the rotation constants so a developer's environment variables cannot change test outcomes."""
    for name, value in ROTATION_DEFAULTS.items():
        patcher = mock.patch.object(config, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
