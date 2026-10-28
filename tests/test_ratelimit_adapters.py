"""The rate limit as the two adapters apply it.

``SharedScenarios`` is written once against a small driver interface and run on Telegram and on Bale, so "both
platforms behave identically" is checked rather than assumed. Providers and downloads are mocked at the boundary;
``provider_calls`` lists every request that would have reached Groq, Gemini, Tavily or one of the free APIs.
"""
import json
import re
from types import SimpleNamespace
from unittest import mock

import config
from bot import bale_handler as bh
from bot import messages
from bot import telegram_handler as th
from core import ratelimit
from core.ratelimit import RateLimiter
from core.session import PLATFORM_BALE, PLATFORM_TELEGRAM, sessions
from services import quiz_service
from tests.helpers import BaseTestCase, FakeClock, patch_bot, tg_call, tg_message

LIMIT = 2
WINDOW = 60.0

QUIZ_JSON = json.dumps({
    "question": "Pick the synonym of 'ubiquitous'.",
    "options": ["rare", "widespread", "fragile", "ancient"],
    "answer": "B",
    "explanation": "ubiquitous یعنی همه‌جا حاضر.",
})

# Commands that call Groq, Gemini or Tavily, and commands that cost nothing, as both platforms understand them.
METERED_COMMANDS = ("/debug x = 1", "/read https://example.com", "/arxiv_full 1706.03762", "/quiz grammar")
EXEMPT_COMMANDS = (
    "/start", "/clear", "/fast", "/smart", "/gemini", "/memory_on", "/memory_off", "/exam_on", "/exam_off",
    "/long_on", "/long_off", "/quiz_stop", "/wiki python", "/arxiv attention",
)
NOTICE_SECONDS = re.compile(r"(\d+) ثانیه")


# ---------------------------------------------------------------- platform drivers
class TelegramDriver:
    """Feeds one user action to ``TelegramApp`` (handlers are called directly, like the other Telegram tests)."""

    platform = PLATFORM_TELEGRAM

    def build_app(self):
        self.app = th.TelegramApp(config.TELEGRAM_BOT_TOKEN)
        self.rec = patch_bot(
            self, self.app,
            get_file=SimpleNamespace(file_path="docs/file_1.pdf"), download_file=b"%PDF-1.4 content",
        )
        self.patch_provider(th, "run_chat", "chat", "answer")
        self.patch_provider(th, "transcribe_audio", "stt", "سلام")
        self.patch_provider(th, "run_tool", "tool", "tool answer")
        self.patch_provider(th, "quick_web_answer", "web", "web answer")
        self.patch_provider(th, "fetch_web_context", "testweb", "web data")

    def text(self, user, body="hello", chat=None):
        self.app.on_text(tg_message(chat=chat or user, user=user, text=body))

    def voice(self, user, chat=None):
        voice = SimpleNamespace(duration=5, file_id="V1")
        self.app.on_voice(tg_message(chat=chat or user, user=user, text=None, audio=None, voice=voice))

    def photo(self, user, chat=None):
        photo = [SimpleNamespace(file_id="P1")]
        self.app.on_photo(tg_message(chat=chat or user, user=user, text=None, caption="", photo=photo))

    def pdf(self, user, chat=None):
        doc = SimpleNamespace(file_name="a.pdf", mime_type="application/pdf", file_size=100, file_id="D1")
        self.app.on_document(tg_message(chat=chat or user, user=user, text=None, caption="", document=doc))

    def command(self, user, line, chat=None):
        """Dispatch like the command registration in ``TelegramApp._register_handlers`` does."""
        message = tg_message(chat=chat or user, user=user, text=line)
        cmd, _ = th.split_command(line)
        if cmd in ("start", "panel", "menu"):
            self.app.on_start(message)
        elif cmd == "help":
            self.app.on_help(message)
        elif cmd == "clear":
            self.app.on_clear(message)
        elif cmd in th.SETTING_ACTIONS:
            self.app._make_setting_handler(cmd)(message)
        elif cmd in ("web", "search"):
            self.app.on_web(message)
        elif cmd == "testweb":
            self.app.on_testweb(message)
        elif cmd in th.TOOLS:
            self.app.on_tool(message)
        elif cmd == "quiz":
            self.app.on_quiz(message)
        elif cmd == "quiz_stop":
            self.app.on_quiz_stop(message)
        else:
            self.app.on_text(message)

    def sent_texts(self):
        return self.rec.texts("reply_to") + self.rec.texts("send_message")

    def downloads(self):
        return len(self.rec.of("download_file"))


class BaleDriver:
    """Feeds one user action to ``BaleApp.handle_message``."""

    platform = PLATFORM_BALE

    def build_app(self):
        self.app = bh.BaleApp(config.BALE_BOT_TOKEN)
        self.sent, self.download_calls = [], []

        def download(file_id):
            self.download_calls.append(file_id)
            return b"%PDF-1.4 content", "docs/file_1.pdf"

        self.start_patch(mock.patch.object(self.app, "send", lambda chat_id, text: self.sent.append((chat_id, text))))
        self.start_patch(mock.patch.object(self.app, "download", download))
        self.patch_provider(bh, "run_chat", "chat", "answer")
        self.patch_provider(bh, "transcribe_audio", "stt", "سلام")
        self.patch_provider(bh, "run_tool", "tool", "tool answer")

    @staticmethod
    def message(user, chat=None, **fields):
        return {"chat": {"id": chat or user}, "from": {"id": user}, **fields}

    def text(self, user, body="hello", chat=None):
        self.app.handle_message(self.message(user, chat, text=body))

    def voice(self, user, chat=None):
        self.app.handle_message(self.message(user, chat, voice={"file_id": "V1", "duration": 5000}))

    def photo(self, user, chat=None):
        self.app.handle_message(self.message(user, chat, photo=[{"file_id": "P1", "width": 10, "height": 10}]))

    def pdf(self, user, chat=None):
        doc = {"file_id": "D1", "file_name": "a.pdf", "mime_type": "application/pdf", "file_size": 100}
        self.app.handle_message(self.message(user, chat, document=doc))

    def command(self, user, line, chat=None):
        self.text(user, line, chat)

    def sent_texts(self):
        return [text for _, text in self.sent]

    def downloads(self):
        return len(self.download_calls)


# ---------------------------------------------------------------- fixture and shared scenarios
class LimitFixture:
    """A limiter on a fake clock (limit 2 per 60 s), mocked providers, and helpers. Needs a driver mixed in."""

    def setUp(self):
        super().setUp()
        self.provider_calls = []
        self.clock = FakeClock()
        self.limiter = RateLimiter(clock=self.clock)
        for patcher in (
            mock.patch.object(ratelimit, "limiter", self.limiter),
            mock.patch.object(config, "RATE_LIMIT_MAX", LIMIT),
            mock.patch.object(config, "RATE_LIMIT_WINDOW_SECONDS", WINDOW),
            mock.patch.object(quiz_service, "process_ai_query", self.recording("quiz", (QUIZ_JSON, ""))),
        ):
            self.start_patch(patcher)
        self.build_app()

    def start_patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def recording(self, name, result):
        def function(*args, **kwargs):
            self.provider_calls.append(name)
            return result
        return function

    def patch_provider(self, module, attribute, name, result):
        self.start_patch(mock.patch.object(module, attribute, self.recording(name, result)))

    def notices(self):
        return [text for text in self.sent_texts() if text.startswith("⏳") and NOTICE_SECONDS.search(text)]

    def exhaust(self, user=111):
        """Use up the whole budget of ``user`` with plain text; no notice is produced."""
        for _ in range(LIMIT):
            self.text(user)
        self.assertEqual(self.notices(), [])

    def session(self, chat=111):
        return sessions.get(self.platform, chat)


class SharedScenarios(LimitFixture):
    """Behaviour that must be identical on every platform."""

    # ------------------------------------------------------------ boundary, notice, expiry
    def test_text_is_answered_up_to_the_limit_and_dropped_after(self):
        for _ in range(LIMIT):
            self.text(111)
        self.assertEqual(self.provider_calls, ["chat"] * LIMIT)
        self.text(111)
        self.assertEqual(self.provider_calls, ["chat"] * LIMIT)

    def test_notice_is_short_persian_and_says_how_many_seconds_to_wait(self):
        self.text(111)                 # t=0
        self.clock.advance(20)
        self.text(111)                 # t=20: budget used up
        self.clock.advance(5)
        self.text(111)                 # t=25: blocked until the t=0 request is a minute old
        (notice,) = self.notices()
        self.assertEqual(notice, messages.rate_limited_text(35))
        self.assertEqual(NOTICE_SECONDS.search(notice).group(1), "35")
        self.assertLess(len(notice), 100)

    def test_notice_is_sent_once_and_further_messages_are_silently_dropped(self):
        self.exhaust()
        sent_before = len(self.sent_texts())
        for _ in range(10):
            self.text(111)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(len(self.sent_texts()), sent_before + 1)  # the notice, nothing else
        self.assertEqual(self.provider_calls, ["chat"] * LIMIT)

    def test_a_new_notice_needs_a_full_window_after_the_previous_one(self):
        self.exhaust()
        self.clock.advance(1)
        self.text(111)                              # t=1: notice
        self.clock.advance(WINDOW - 0.5)            # t=60.5: the window has slid past both accepted requests
        for _ in range(LIMIT):
            self.text(111)
        self.text(111)                              # t=60.5: blocked again, but only 59.5 s after the notice
        self.assertEqual(len(self.notices()), 1)
        self.clock.advance(0.5)
        self.text(111)                              # t=61: a whole window since the notice
        self.assertEqual(len(self.notices()), 2)

    def test_the_user_can_ask_again_when_the_window_has_passed(self):
        self.exhaust()
        self.text(111)
        self.clock.advance(WINDOW)
        self.text(111)
        self.assertEqual(self.provider_calls, ["chat"] * (LIMIT + 1))

    # ------------------------------------------------------------ whose bucket
    def test_users_have_separate_budgets(self):
        self.exhaust(user=111)
        self.text(222)
        self.assertEqual(self.provider_calls, ["chat"] * (LIMIT + 1))

    def test_the_limit_follows_the_user_not_the_chat(self):
        for _ in range(2):             # two people in one group chat, each within their own budget
            self.text(111, chat=900)
            self.text(222, chat=900)
        self.assertEqual(self.provider_calls, ["chat"] * 4)
        self.assertEqual(self.notices(), [])

        self.text(111, chat=901)       # the same person in another chat has already spent their budget
        self.assertEqual(self.provider_calls, ["chat"] * 4)
        self.assertEqual(len(self.notices()), 1)

    def test_every_counted_action_follows_the_user_when_several_people_share_a_chat(self):
        actions = {
            "text": lambda user: self.text(user, chat=900),
            "voice": lambda user: self.voice(user, chat=900),
            "photo": lambda user: self.photo(user, chat=900),
            "pdf": lambda user: self.pdf(user, chat=900),
        }
        for line in METERED_COMMANDS:
            actions[line] = lambda user, line=line: self.command(user, line, chat=900)
        for name, act in actions.items():
            with self.subTest(action=name):
                self.limiter.reset()
                notices = len(self.notices())
                for user in (111, 222):
                    for _ in range(LIMIT):
                        act(user)
                self.assertEqual(len(self.notices()), notices, "two people in one chat shared a budget")
                act(111)
                self.assertEqual(len(self.notices()), notices + 1)

    def test_the_same_id_on_the_other_platform_never_shares_the_bucket(self):
        other = PLATFORM_BALE if self.platform == PLATFORM_TELEGRAM else PLATFORM_TELEGRAM
        for _ in range(LIMIT):
            self.assertTrue(self.limiter.check(other, 111).allowed)
        self.assertFalse(self.limiter.check(other, 111).allowed)  # their budget there is gone
        self.exhaust(user=111)                                     # this platform's budget is untouched
        self.assertEqual(self.provider_calls, ["chat"] * LIMIT)

    # ------------------------------------------------------------ what counts
    def test_voice_photo_and_pdf_are_blocked_before_any_download_or_provider_call(self):
        for kind in ("voice", "photo", "pdf"):
            with self.subTest(kind=kind):
                self.limiter.reset()
                send = getattr(self, kind)
                downloads, calls, notices = self.downloads(), len(self.provider_calls), len(self.notices())
                for _ in range(LIMIT):
                    send(111)
                self.assertEqual(self.downloads() - downloads, LIMIT)      # within the limit: downloaded and answered
                self.assertGreater(len(self.provider_calls), calls)
                downloads, calls = self.downloads(), len(self.provider_calls)
                send(111)                                                  # over the limit
                self.assertEqual(self.downloads(), downloads, "downloaded although over the limit")
                self.assertEqual(len(self.provider_calls), calls, "called a provider although over the limit")
                self.assertEqual(len(self.notices()), notices + 1)

    def test_a_voice_message_is_one_request_even_though_it_calls_two_providers(self):
        self.voice(111)
        self.assertEqual(self.provider_calls, ["stt", "chat"])  # speech-to-text, then the answer
        self.voice(111)
        self.voice(111)
        self.assertEqual(self.provider_calls, ["stt", "chat"] * 2)
        self.assertEqual(len(self.notices()), 1)

    def test_text_voice_photo_and_pdf_share_one_budget(self):
        self.photo(111)
        self.pdf(111)
        self.text(111)
        self.voice(111)
        self.assertEqual(self.provider_calls.count("chat"), LIMIT)
        self.assertEqual(len(self.notices()), 1)

    def test_every_command_that_calls_a_provider_is_counted(self):
        for line in METERED_COMMANDS:
            with self.subTest(command=line):
                self.limiter.reset()
                self.provider_calls.clear()
                notices = len(self.notices())
                for _ in range(LIMIT):
                    self.command(111, line)
                calls = list(self.provider_calls)
                self.assertTrue(calls, "the command never reached a provider")
                self.command(111, line)                                    # over the limit
                self.assertEqual(self.provider_calls, calls, "a provider was called although over the limit")
                self.assertEqual(len(self.notices()), notices + 1)

    def test_commands_that_cost_nothing_are_never_counted(self):
        for _ in range(LIMIT * 4):
            for line in EXEMPT_COMMANDS:
                self.command(111, line)
        self.assertEqual(self.notices(), [])
        self.assertEqual(len(self.limiter), 0, "an exempt action was counted")
        self.exhaust()                                                     # the whole budget is still there
        self.assertEqual(self.provider_calls.count("chat"), LIMIT)

    def test_commands_that_cost_nothing_still_work_for_a_user_who_is_over_the_limit(self):
        self.exhaust()
        self.text(111)                                           # over the limit now: one notice
        sent_before = len(self.sent_texts())
        self.command(111, "/smart")
        self.command(111, "/wiki python")
        self.command(111, "/arxiv attention")
        self.command(111, "/clear")
        self.assertEqual(self.session().model, config.MODEL_SMART)
        self.assertEqual(self.provider_calls.count("tool"), 2)  # the free tools really ran
        self.assertEqual(len(self.sent_texts()), sent_before + 4)
        self.assertEqual(len(self.notices()), 1)                 # nothing was dropped or announced

    def test_a_typed_quiz_answer_is_graded_for_free_and_a_new_question_is_counted(self):
        self.command(111, "/quiz")                               # counts (1/2): asks the AI
        self.assertEqual(self.provider_calls, ["quiz"])
        self.text(111, "B")                                      # typed answer: graded locally, not counted
        self.text(111, "B")                                      # no quiz is active any more: this is free text
        self.assertEqual(self.provider_calls, ["quiz", "chat"])  # counts (2/2)
        self.assertEqual(self.notices(), [])
        self.command(111, "/quiz")                               # over the limit
        self.assertEqual(self.provider_calls, ["quiz", "chat"])
        self.assertEqual(len(self.notices()), 1)

    def test_a_typed_quiz_answer_is_graded_even_when_the_user_is_over_the_limit(self):
        self.command(111, "/quiz")
        self.text(111)                                           # budget used up: /quiz + this message
        self.text(111)                                           # blocked, notice sent
        self.assertEqual(len(self.notices()), 1)
        sent_before = len(self.sent_texts())
        self.text(111, "B")                                      # still graded
        self.assertEqual(len(self.sent_texts()), sent_before + 1)
        self.assertIn("✅ درست", self.sent_texts()[-1])

    # ------------------------------------------------------------ switched off / allow-list
    def test_rate_limit_zero_disables_everything(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", 0):
            for _ in range(60):
                self.text(111)
            self.voice(111)
            self.command(111, "/debug x")
        self.assertEqual(self.provider_calls.count("chat"), 61)
        self.assertEqual(self.provider_calls.count("tool"), 1)
        self.assertEqual(self.notices(), [])
        self.assertEqual(len(self.limiter), 0)

    def test_users_outside_the_allow_list_never_reach_the_limiter(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            for _ in range(10):
                self.text(999)
                self.voice(999)
            self.assertEqual(len(self.limiter), 0)
            self.assertEqual(self.provider_calls, [])
            self.assertEqual(self.notices(), [])
            self.exhaust(user=111)                               # the real user's budget is untouched
            self.assertEqual(self.provider_calls, ["chat"] * LIMIT)

    def test_the_limit_applies_to_allowed_users_too(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            self.exhaust(user=111)
            self.text(111)
        self.assertEqual(len(self.notices()), 1)


class TelegramLimitTests(SharedScenarios, TelegramDriver, BaseTestCase):
    pass


class BaleLimitTests(SharedScenarios, BaleDriver, BaseTestCase):
    pass


# ---------------------------------------------------------------- platform-specific entry points
class TelegramOnlyTests(LimitFixture, TelegramDriver, BaseTestCase):
    """Telegram has more entry points (buttons, /web, /testweb, /panel) than Bale."""

    def test_web_search_commands_are_counted(self):
        for line in ("/web dollar price", "/search dollar price", "/testweb dollar"):
            with self.subTest(command=line):
                self.limiter.reset()
                self.provider_calls.clear()
                notices = len(self.notices())
                self.command(111, line)
                self.command(111, line)
                self.assertEqual(len(self.provider_calls), 2)
                self.command(111, line)                          # over the limit
                self.assertEqual(len(self.provider_calls), 2)
                self.assertEqual(len(self.notices()), notices + 1)

    def test_help_panel_and_menu_are_free(self):
        for _ in range(LIMIT * 3):
            for line in ("/help", "/panel", "/menu", "/start"):
                self.command(111, line)
        self.assertEqual(len(self.limiter), 0)
        self.assertEqual(self.notices(), [])

    def test_panel_buttons_are_free(self):
        for _ in range(LIMIT * 3):
            for action in ("fast", "smart", "gemini", "memory_on", "exam_on", "long_on"):
                self.app.on_panel_callback(tg_call(f"cmd:{action}"))
        self.assertEqual(len(self.limiter), 0)
        self.assertEqual(self.notices(), [])

    def test_quiz_answer_buttons_are_free_and_the_next_button_is_counted(self):
        self.command(111, "/quiz")                               # 1/2
        self.app.on_quiz_callback(tg_call(f"quiz:B:{self.session().quiz['id']}"))  # graded locally
        self.app.on_quiz_callback(tg_call("quiz:next"))          # 2/2: a new question
        self.assertEqual(self.provider_calls, ["quiz", "quiz"])  # had the answer been counted, this would be blocked
        self.assertEqual(self.notices(), [])
        self.app.on_quiz_callback(tg_call("quiz:next"))          # 3/2
        self.assertEqual(self.provider_calls, ["quiz", "quiz"])
        self.assertEqual(len(self.notices()), 1)

    def test_next_button_over_the_limit_answers_the_tap_keeps_the_buttons_and_notices_once(self):
        self.exhaust()
        self.rec.calls.clear()
        self.app.on_quiz_callback(tg_call("quiz:next"))
        self.app.on_quiz_callback(tg_call("quiz:next"))
        self.assertEqual(self.provider_calls.count("quiz"), 0)
        self.assertEqual(len(self.rec.of("answer_callback_query")), 2)  # the spinner stops both times
        self.assertEqual(self.rec.of("edit_message_reply_markup"), [])  # the button stays for a later tap
        self.assertEqual(len(self.notices()), 1)

    def test_the_notice_is_a_reply_to_the_offending_message(self):
        self.exhaust()
        message = tg_message(chat=111, user=111, text="again")
        self.app.on_text(message)
        notice_call = [c for c in self.rec.of("reply_to") if NOTICE_SECONDS.search(c[1][1])][0]
        self.assertIs(notice_call[1][0], message)

    def test_a_document_over_the_limit_gets_only_the_notice_even_if_it_is_not_a_pdf(self):
        self.exhaust()
        doc = SimpleNamespace(file_name="a.zip", mime_type="application/zip", file_size=10, file_id="Z")
        self.app.on_document(tg_message(user=111, chat=111, text=None, caption="", document=doc))
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn("PDF", self.sent_texts()[-1])


class BaleOnlyTests(LimitFixture, BaleDriver, BaseTestCase):
    def test_without_a_sender_the_chat_id_is_the_user(self):
        for chat in (70, 71):
            for _ in range(LIMIT):
                self.app.handle_message({"chat": {"id": chat}, "text": "hi"})
        self.assertEqual(self.provider_calls, ["chat"] * (2 * LIMIT))
        self.assertEqual(len(self.limiter), 2)
        self.app.handle_message({"chat": {"id": 70}, "text": "hi"})
        self.assertEqual(len(self.notices()), 1)

    def test_handle_tools_called_without_a_user_id_falls_back_to_the_chat(self):
        for _ in range(LIMIT):
            self.assertTrue(self.app.handle_tools(80, "/debug x"))
        self.assertTrue(self.app.handle_tools(80, "/debug x"))   # handled (dropped) and announced
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.provider_calls.count("tool"), LIMIT)

    def test_unknown_commands_go_to_the_ai_and_therefore_count(self):
        for _ in range(LIMIT + 1):
            self.command(111, "/nosuchcommand")
        self.assertEqual(self.provider_calls, ["chat"] * LIMIT)
        self.assertEqual(len(self.notices()), 1)
