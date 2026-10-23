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
