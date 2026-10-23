import json
from types import SimpleNamespace
from unittest import mock

import config
from bot import messages
from bot import telegram_handler as th
from core.session import PLATFORM_TELEGRAM, sessions
from services import clients, quiz_service
from services.media_service import PDF_DEFAULT_PROMPT
from services.tools import TOOLS
from tests.helpers import BaseTestCase, make_api_error, patch_bot, tg_call, tg_message

QUIZ_JSON = json.dumps({
    "question": "Pick the synonym of 'ubiquitous'.",
    "options": ["rare", "widespread", "fragile", "ancient"],
    "answer": "B",
    "explanation": "ubiquitous یعنی همه‌جا حاضر.",
})


class TelegramTestCase(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = th.TelegramApp(config.TELEGRAM_BOT_TOKEN)
        self.rec = patch_bot(
            self, self.app,
            get_file=SimpleNamespace(file_path="docs/file_1.pdf"),
            download_file=b"%PDF-1.4 content",
        )

    def session(self, chat=1):
        return sessions.get(PLATFORM_TELEGRAM, chat)

    def replies(self):
        return self.rec.texts("reply_to")

    @staticmethod
    def buttons(markup):
        return [b for row in markup.keyboard for b in row]


class WiringTests(TelegramTestCase):
    def test_catch_all_handler_is_registered_last(self):
        handlers = getattr(self.app.bot, "message_handlers", None)
        if not handlers or not isinstance(handlers[-1], dict):
            self.skipTest("this telebot version exposes handlers differently")
        self.assertEqual(handlers[-1]["function"], self.app.on_text)

    def test_every_tool_command_is_registered(self):
        handlers = getattr(self.app.bot, "message_handlers", None)
        if not handlers or not isinstance(handlers[0], dict):
            self.skipTest("this telebot version exposes handlers differently")
        registered = {c for h in handlers for c in (h["filters"].get("commands") or [])}
        self.assertTrue(set(TOOLS) <= registered)
        self.assertTrue({"start", "panel", "menu", "help", "clear", "web", "quiz", "quiz_stop", "fast", "memory_on"} <= registered)


class PanelTests(TelegramTestCase):
    def test_start_shows_state_and_a_complete_keyboard(self):
        self.app.on_start(tg_message(text="/start"))
        text = self.replies()[0]
        self.assertIn("❌ خاموش", text)  # memory is OFF by default
        markup = self.rec.of("reply_to")[0][2]["reply_markup"]
        self.assertEqual(
            [b.callback_data for b in self.buttons(markup)],
            ["cmd:fast", "cmd:smart", "cmd:gemini", "cmd:memory_on", "cmd:memory_off",
             "cmd:exam_on", "cmd:exam_off", "cmd:long_on", "cmd:long_off"],
        )
        marked = [b.text for b in self.buttons(markup) if b.text.startswith("✅")]
        self.assertEqual(marked, ["✅ ⚡️ سریع", "✅ 💾 حافظه خاموش", "✅ 🎯 آزمون خاموش", "✅ 📜 بلند خاموش"])
        self.assertTrue(all(len(b.callback_data) <= 64 for b in self.buttons(markup)))

    def test_welcome_fits_in_a_single_message(self):
        self.assertLess(len(th.welcome_text(self.session())), config.TG_CHUNK_SIZE)

    def test_panel_and_menu_commands_reuse_the_welcome(self):
        self.app.on_start(tg_message(text="/panel"))
        self.assertEqual(len(self.replies()), 1)

    def test_button_press_updates_state_toast_and_message(self):
        self.app.on_panel_callback(tg_call("cmd:memory_on"))
        self.assertTrue(self.session().memory_on)
        self.assertEqual([c[0] for c in self.rec.calls], ["answer_callback_query", "edit_message_text"])
        self.assertEqual(self.rec.of("answer_callback_query")[0][1][1], messages.PANEL_TOASTS["memory_on"])
        edit = self.rec.of("edit_message_text")[0]
        self.assertIn("✅ روشن", edit[1][0])
        self.assertTrue(self.buttons(edit[2]["reply_markup"])[3].text.startswith("✅"))

    def test_every_action_button_works(self):
        for action, check in (("smart", lambda s: s.model == config.MODEL_SMART),
                              ("gemini", lambda s: s.model == config.MODEL_GEMINI),
                              ("exam_on", lambda s: s.exam_mode),
                              ("long_on", lambda s: s.long_mode)):
            with self.subTest(action=action):
                self.app.on_panel_callback(tg_call(f"cmd:{action}"))
                self.assertTrue(check(self.session()))

    def test_memory_off_button_wipes_history(self):
        s = self.session()
        s.memory_on = True
        s.remember("q", "a")
        self.app.on_panel_callback(tg_call("cmd:memory_off"))
        self.assertFalse(s.memory_on)
        self.assertEqual(s.history, [])

    def test_unauthorized_button_press_changes_nothing(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            self.app.on_panel_callback(tg_call("cmd:memory_on", user=999))
        self.assertFalse(self.session().memory_on)
        self.assertEqual([c[0] for c in self.rec.calls], ["answer_callback_query"])

    def test_unknown_action_is_ignored(self):
        self.app.on_panel_callback(tg_call("cmd:bogus"))
        self.assertEqual([c[0] for c in self.rec.calls], ["answer_callback_query"])

    def test_pressing_the_active_option_does_not_crash(self):
        with mock.patch.object(self.app.bot, "edit_message_text", side_effect=make_api_error("Bad Request: message is not modified")):
            self.app.on_panel_callback(tg_call("cmd:fast"))  # must not raise

    def test_setting_commands_reply_with_the_configured_texts(self):
        for action in ("fast", "smart", "gemini", "exam_on", "exam_off", "long_on", "long_off", "memory_on", "memory_off"):
            with self.subTest(action=action):
                self.rec.calls.clear()
                self.app._make_setting_handler(action)(tg_message(chat=3, text=f"/{action}"))
                self.assertEqual(self.replies(), [messages.TG_ACTION_REPLIES[action]])

    def test_setting_command_changes_state(self):
        self.app._make_setting_handler("smart")(tg_message(chat=4, text="/smart"))
        self.assertEqual(self.session(4).model, config.MODEL_SMART)

    def test_unauthorized_command_is_silent(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            self.app._make_setting_handler("memory_on")(tg_message(user=999, text="/memory_on"))
            self.app.on_start(tg_message(user=999, text="/start"))
            self.app.on_help(tg_message(user=999, text="/help"))
        self.assertEqual(self.rec.calls, [])
        self.assertFalse(self.session().memory_on)

    def test_clear_and_help(self):
        s = self.session()
        s.remember("q", "a")
        self.app.on_clear(tg_message(text="/clear"))
        self.assertEqual(s.history, [])
        self.app.on_help(tg_message(text="/help"))
        help_text = "".join(self.replies())
        for command in ("/arxiv_full", "/quiz_stop", "/panel", "/debug"):
            self.assertIn(command, help_text)


class ReplyRenderingTests(TelegramTestCase):
    def test_short_reply_is_html_formatted(self):
        self.app.reply(tg_message(), "**bold**")
        call = self.rec.of("reply_to")[0]
        self.assertEqual(call[1][1], "<b>bold</b>")
        self.assertEqual(call[2]["parse_mode"], "HTML")

    def test_model_output_is_escaped(self):
        self.app.reply(tg_message(), "if a < b: **x**", escape=True)
        self.assertEqual(self.replies()[0], "if a &lt; b: <b>x</b>")

    def test_long_reply_is_chunked_and_markup_only_on_the_last_chunk(self):
        markup = object()
        self.app.reply(tg_message(), "\n".join(f"line {i}" for i in range(2000)), reply_markup=markup)
        first = self.rec.of("reply_to")
        rest = self.rec.of("send_message")
        self.assertEqual(len(first), 1)
        self.assertGreaterEqual(len(rest), 1)
        self.assertNotIn("reply_markup", first[0][2])
        for call in rest[:-1]:
            self.assertNotIn("reply_markup", call[2])
        self.assertIs(rest[-1][2]["reply_markup"], markup)

    def test_falls_back_to_plain_text_when_telegram_rejects_the_html(self):
        outcomes = [make_api_error(), None]

        def flaky(message, text, **kwargs):
            self.rec.calls.append(("reply_to", (message, text), kwargs))
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome

        with mock.patch.object(self.app.bot, "reply_to", flaky):
            self.app.reply(tg_message(), "**a** <b")
        self.assertEqual(len(self.rec.of("reply_to")), 2)
        self.assertEqual(self.replies()[1], "**a** <b")  # raw text, no parse_mode
        self.assertNotIn("parse_mode", self.rec.of("reply_to")[1][2])

    def test_empty_text_gets_a_placeholder(self):
        self.app.reply(tg_message(), "   ")
        self.assertEqual(self.replies(), [messages.NO_RESPONSE])

    def test_answer_runs_the_pipeline_and_replies_with_escaping(self):
        with mock.patch.object(th, "run_chat", return_value="a < b") as run:
            self.app.answer(tg_message(chat=9), "question", memory_text="mem")
        self.assertEqual(self.rec.of("send_chat_action")[0][1], (9, "typing"))
        self.assertIs(run.call_args.args[0], self.session(9))
        self.assertEqual(run.call_args.kwargs["memory_text"], "mem")
        self.assertEqual(self.replies(), ["a &lt; b"])


class TextAndToolTests(TelegramTestCase):
    def test_plain_text_goes_to_the_pipeline(self):
        with mock.patch.object(th, "run_chat", return_value="ANS") as run:
            self.app.on_text(tg_message(text="سلام"))
        self.assertEqual(run.call_args.args[1], "سلام")
        self.assertEqual(self.replies(), ["ANS"])

    def test_unauthorized_text_is_ignored(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]), mock.patch.object(th, "run_chat") as run:
            self.app.on_text(tg_message(user=999, text="hi"))
        run.assert_not_called()

    def test_tool_command_uses_the_session_model(self):
        self.session().model = config.MODEL_SMART
        with mock.patch.object(th, "run_tool", return_value="TOOL OUT") as run:
            self.app.on_tool(tg_message(text="/debug@mybot NameError: x"))
        self.assertEqual(run.call_args.args, ("debug", "NameError: x", config.MODEL_SMART))
        self.assertEqual(self.replies(), ["TOOL OUT"])

    def test_web_command(self):
        with mock.patch.object(th, "quick_web_answer", return_value="WEB ANSWER"):
            self.app.on_web(tg_message(text="/web قیمت طلا"))
        self.assertEqual(self.replies(), ["WEB ANSWER"])

    def test_web_command_without_query_and_with_failure(self):
        self.app.on_web(tg_message(text="/web"))
        self.assertIn("عبارت", self.replies()[0])
        with mock.patch.object(th, "quick_web_answer", side_effect=RuntimeError(f"bad {config.GEMINI_API_KEY}")):
            self.app.on_web(tg_message(text="/web x"))
        self.assertNotIn(config.GEMINI_API_KEY, self.replies()[-1])

    def test_testweb_escapes_raw_search_output(self):
        with mock.patch.object(th, "fetch_web_context", return_value="<script>x</script>"):
            self.app.on_testweb(tg_message(text="/testweb"))
        self.assertIn("&lt;script&gt;", self.replies()[0])


class QuizTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(quiz_service, "process_ai_query", return_value=(QUIZ_JSON, ""))
        p.start()
        self.addCleanup(p.stop)

    def ask(self, chat=1):
        self.app.on_quiz(tg_message(chat=chat, text="/quiz ielts"))
        return self.rec.of("reply_to")[-1][2]["reply_markup"]

    def test_question_comes_with_abcd_buttons_carrying_the_question_id(self):
        markup = self.ask()
        self.assertEqual([b.callback_data for b in self.buttons(markup)], [f"quiz:{l}:1" for l in "ABCD"])

    def test_correct_button_press(self):
        self.ask()
        self.rec.calls.clear()
        self.app.on_quiz_callback(tg_call("quiz:B:1"))
        self.assertEqual([c[0] for c in self.rec.calls], ["answer_callback_query", "edit_message_reply_markup", "reply_to"])
        self.assertEqual(self.rec.calls[0][1][1], "✅ درست")
        next_button = self.buttons(self.rec.calls[2][2]["reply_markup"])[0]
        self.assertEqual(next_button.callback_data, "quiz:next")

    def test_wrong_button_press_shows_the_right_answer(self):
        self.ask()
        self.rec.calls.clear()
        self.app.on_quiz_callback(tg_call("quiz:A:1"))
        self.assertEqual(self.rec.calls[0][1][1], "❌ نادرست")
        self.assertIn("پاسخ صحیح: B) widespread", self.rec.texts("reply_to")[0])

    def test_double_tap_and_stale_buttons_are_rejected(self):
        self.ask()
        self.app.on_quiz_callback(tg_call("quiz:B:1"))
        self.rec.calls.clear()
        self.app.on_quiz_callback(tg_call("quiz:B:1"))
        self.assertEqual(len(self.rec.calls), 1)
        self.assertIn("دیگر فعال نیست", self.rec.calls[0][1][1])

    def test_next_button_clears_buttons_and_asks_again(self):
        self.ask()
        self.app.on_quiz_callback(tg_call("quiz:B:1"))
        self.rec.calls.clear()
        self.app.on_quiz_callback(tg_call("quiz:next"))
        self.assertIn("edit_message_reply_markup", [c[0] for c in self.rec.calls])
        markup = self.rec.of("reply_to")[0][2]["reply_markup"]
        self.assertEqual(self.buttons(markup)[0].callback_data, "quiz:A:2")

    def test_malformed_callback_data_is_ignored(self):
        self.ask()
        for data in ("quiz:Z:1", "quiz:A", "quiz:A:x", "quiz:"):
            with self.subTest(data=data):
                self.rec.calls.clear()
                self.app.on_quiz_callback(tg_call(data))
                self.assertEqual([c[0] for c in self.rec.calls], ["answer_callback_query"])
                self.assertEqual(self.rec.calls[0][1], ("cb-1", None))  # spinner dismissed, nothing else happens

    def test_unauthorized_press_is_ignored(self):
        self.ask()
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            self.rec.calls.clear()
            self.app.on_quiz_callback(tg_call("quiz:B:1", user=999))
        self.assertEqual([c[0] for c in self.rec.calls], ["answer_callback_query"])
        self.assertTrue(quiz_service.has_active_quiz(self.session()))

    def test_typed_letter_answers_the_active_question(self):
        self.ask()
        self.rec.calls.clear()
        with mock.patch.object(th, "run_chat") as run:
            self.app.on_text(tg_message(text="b"))
        run.assert_not_called()
        self.assertIn("✅ درست", self.replies()[0])
        self.assertIsNotNone(self.rec.of("reply_to")[0][2]["reply_markup"])

    def test_typed_letter_without_active_question_is_normal_chat(self):
        with mock.patch.object(th, "run_chat", return_value="ANS") as run:
            self.app.on_text(tg_message(text="b"))
        run.assert_called_once()

    def test_quiz_stop(self):
        self.ask()
        self.app.on_quiz_callback(tg_call("quiz:A:1"))
        self.rec.calls.clear()
        self.app.on_quiz_stop(tg_message(text="/quiz_stop"))
        self.assertIn("امتیاز نهایی: 0 از 1", self.replies()[0])


class DocumentTests(TelegramTestCase):
    @staticmethod
    def doc_message(name="slides.pdf", mime="application/pdf", size=1000, caption=None, user=111):
        return tg_message(
            user=user, text=None, caption=caption,
            document=SimpleNamespace(file_name=name, mime_type=mime, file_size=size, file_id="F1"),
        )

    def test_pdf_with_caption_is_sent_to_the_pipeline(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message(caption="تمرین‌های فصل ۳ را حل کن"))
        args, kwargs = answer.call_args
        self.assertEqual(args[1], "تمرین‌های فصل ۳ را حل کن")
        self.assertEqual(kwargs["image"], {"data": b"%PDF-1.4 content", "mime": "application/pdf"})
        self.assertEqual(kwargs["memory_text"], "[PDF: slides.pdf] تمرین‌های فصل ۳ را حل کن")

    def test_pdf_without_caption_uses_the_summary_prompt(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message())
        self.assertEqual(answer.call_args.args[1], PDF_DEFAULT_PROMPT)

    def test_exam_mode_uses_the_exam_prompt(self):
        self.session().exam_mode = True
        with mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message())
        self.assertIn("RESULT", answer.call_args.args[1])

    def test_pdf_detected_by_extension_when_mime_is_missing(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message(name="NOTES.PDF", mime=None))
        answer.assert_called_once()

    def test_other_file_types_are_refused(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message(name="a.docx", mime="application/msword"))
        answer.assert_not_called()
        self.assertEqual(self.replies(), ["📄 فعلاً فقط فایل PDF پشتیبانی می‌شود."])

    def test_oversize_file_is_refused_before_downloading(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message(size=config.MAX_PDF_BYTES + 1))
        answer.assert_not_called()
        self.assertEqual(self.rec.of("get_file"), [])

    def test_unauthorized_user_is_ignored(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]), mock.patch.object(self.app, "answer") as answer:
            self.app.on_document(self.doc_message(user=999))
        answer.assert_not_called()

    def test_without_gemini_key_the_user_is_told(self):
        with mock.patch.object(clients, "gemini_client", None):
            self.app.on_document(self.doc_message())
        self.assertIn("GEMINI_API_KEY", self.replies()[0])

    def test_download_errors_never_leak_the_bot_token(self):
        with mock.patch.object(self.app.bot, "get_file", side_effect=RuntimeError(f"url with {config.TELEGRAM_BOT_TOKEN}")):
            self.app.on_document(self.doc_message())
        self.assertNotIn(config.TELEGRAM_BOT_TOKEN, self.replies()[0])
        self.assertIn("خطا در دریافت فایل", self.replies()[0])


class VoiceAndPhotoTests(TelegramTestCase):
    @staticmethod
    def voice_message(duration=3):
        return tg_message(text=None, audio=None, voice=SimpleNamespace(duration=duration, file_id="V1"))

    def test_voice_is_transcribed_echoed_and_answered(self):
        with mock.patch.object(th, "transcribe_audio", return_value="سلام <دنیا>") as stt, \
                mock.patch.object(self.app, "answer") as answer:
            self.app.on_voice(self.voice_message())
        self.assertEqual(stt.call_args.args[1], "audio.ogg")  # unsupported extension -> safe default
        self.assertIn("🎙️ <b>متن شنیده‌شده:</b>\nسلام &lt;دنیا&gt;", self.replies()[0])
        self.assertEqual(answer.call_args.args[1], "سلام <دنیا>")

    def test_telegram_duration_is_in_seconds(self):
        with mock.patch.object(th, "transcribe_audio", return_value="hi"), mock.patch.object(self.app, "answer"):
            self.app.on_voice(self.voice_message(duration=300))
            self.assertEqual(len(self.replies()), 1)  # exactly 5 minutes is fine
            self.app.on_voice(self.voice_message(duration=301))
        self.assertIn("بیش از 5 دقیقه", self.replies()[-1])

    def test_empty_transcription(self):
        with mock.patch.object(th, "transcribe_audio", return_value=""), mock.patch.object(self.app, "answer") as answer:
            self.app.on_voice(self.voice_message())
        answer.assert_not_called()
        self.assertIn("تشخیص داده نشد", self.replies()[0])

    def test_transcription_error_is_redacted(self):
        with mock.patch.object(th, "transcribe_audio", side_effect=RuntimeError(f"401 {config.GROQ_API_KEY}")):
            self.app.on_voice(self.voice_message())
        self.assertNotIn(config.GROQ_API_KEY, self.replies()[0])

    def test_photo_is_sent_to_gemini_with_its_mime_type(self):
        message = tg_message(text=None, caption="حل کن", photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")])
        with mock.patch.object(self.app.bot, "get_file", return_value=SimpleNamespace(file_path="photos/a.png")), \
                mock.patch.object(self.app, "answer") as answer:
            self.app.on_photo(message)
        self.assertEqual(self.rec.of("download_file")[0][1], ("photos/a.png",))
        self.assertEqual(answer.call_args.kwargs["image"]["mime"], "image/png")
        self.assertEqual(answer.call_args.kwargs["memory_text"], "[تصویر] حل کن")

    def test_photo_without_gemini_key(self):
        with mock.patch.object(clients, "gemini_client", None):
            self.app.on_photo(tg_message(text=None, photo=[SimpleNamespace(file_id="x")]))
        self.assertIn("GEMINI_API_KEY", self.replies()[0])
