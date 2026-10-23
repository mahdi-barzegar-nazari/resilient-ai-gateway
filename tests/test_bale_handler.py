import json
from unittest import mock

import config
from bot import bale_handler as bh
from bot import messages
from core.session import PLATFORM_BALE, PLATFORM_TELEGRAM, sessions
from services import clients, quiz_service
from services.media_service import PDF_DEFAULT_PROMPT
from tests.helpers import BaseTestCase, FakeResponse

QUIZ_JSON = json.dumps({
    "question": "Pick the synonym of 'ubiquitous'.",
    "options": ["rare", "widespread", "fragile", "ancient"],
    "answer": "B",
    "explanation": "ubiquitous یعنی همه‌جا حاضر.",
})


def message(chat=60, user=111, **fields):
    return {"chat": {"id": chat}, "from": {"id": user}, **fields}


class BaleTestCase(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = bh.BaleApp(config.BALE_BOT_TOKEN)
        self.sent = []
        p = mock.patch.object(self.app, "send", lambda chat_id, text: self.sent.append((chat_id, text)))
        p.start()
        self.addCleanup(p.stop)

    def session(self, chat=60):
        return sessions.get(PLATFORM_BALE, chat)

    def texts(self):
        return [t for _, t in self.sent]


class VoiceDurationTests(BaleTestCase):
    """Regression: Bale reports voice duration in milliseconds, not seconds."""

    def test_unit_conversion(self):
        self.assertEqual(bh.bale_voice_seconds({"duration": 3000}), 3.0)
        self.assertEqual(bh.bale_voice_seconds({}), 0)
        self.assertEqual(bh.bale_voice_seconds({"duration": None}), 0)

    def run_voice(self, duration):
        voice = {"file_id": "V1"}
        if duration is not None:
            voice["duration"] = duration
        with mock.patch.object(self.app, "download", return_value=(b"OGG", "voices/f.oga")), \
                mock.patch.object(bh, "transcribe_audio", return_value="سلام دنیا") as stt, \
                mock.patch.object(self.app, "answer") as answer:
            self.app.handle_voice(60, voice)
        return stt, answer

    def test_three_second_voice_is_accepted(self):
        stt, answer = self.run_voice(3000)
        stt.assert_called_once()
        self.assertEqual(stt.call_args.args[1], "audio.ogg")
        answer.assert_called_once_with(60, "سلام دنیا")
        self.assertIn("سلام دنیا", self.texts()[0])

    def test_exactly_five_minutes_is_accepted(self):
        _, answer = self.run_voice(300000)
        answer.assert_called_once()

    def test_over_five_minutes_is_rejected_with_the_limit_message(self):
        stt, answer = self.run_voice(300001)
        stt.assert_not_called()
        answer.assert_not_called()
        self.assertEqual(self.texts(), [messages.voice_too_long_text(config.MAX_AUDIO_SECONDS)])

    def test_missing_duration_is_accepted(self):
        _, answer = self.run_voice(None)
        answer.assert_called_once()

    def test_empty_transcription_and_errors(self):
        with mock.patch.object(self.app, "download", return_value=(b"x", "a.oga")), \
                mock.patch.object(bh, "transcribe_audio", return_value=""):
            self.app.handle_voice(60, {"file_id": "V", "duration": 1000})
        self.assertIn("تشخیص داده نشد", self.texts()[-1])
        with mock.patch.object(self.app, "download", side_effect=RuntimeError(f"fail {config.BALE_BOT_TOKEN}")):
            self.app.handle_voice(60, {"file_id": "V", "duration": 1000})
        self.assertNotIn(config.BALE_BOT_TOKEN, self.texts()[-1])


class MessageRoutingTests(BaleTestCase):
    def test_empty_updates_are_ignored(self):
        self.app.handle_message({})
        self.app.handle_message(message(text="   "))
        self.assertEqual(self.sent, [])

    def test_unauthorized_user_gets_their_id_and_nothing_else(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]), mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(chat=61, user=999, text="hello"))
        answer.assert_not_called()
        self.assertIn("دسترسی غیرمجاز", self.texts()[0])
        self.assertIn("999", self.texts()[0])

    def test_allow_listed_chat_is_accepted_even_if_sender_is_not(self):
        with mock.patch.object(config, "ALLOWED_USERS", [61]), mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(chat=61, user=999, text="hello"))
        answer.assert_called_once()

    def test_start_lists_state_and_every_tool(self):
        self.app.handle_message(message(text="/start"))
        welcome = self.texts()[0]
        self.assertIn("❌ خاموش", welcome)  # memory defaults to OFF
        for command in ("/quiz", "/arxiv_full", "/debug", "/read", "/wiki", "PDF"):
            self.assertIn(command, welcome)
        self.assertFalse(self.session().memory_on)

    def test_settings_commands(self):
        for action in ("fast", "smart", "gemini", "exam_on", "exam_off", "long_on", "long_off", "memory_on", "memory_off"):
            with self.subTest(action=action):
                self.app.handle_message(message(text=f"/{action}"))
                self.assertEqual(self.texts()[-1], messages.BALE_ACTION_REPLIES[action])
        self.app.handle_message(message(text="/smart"))
        self.assertEqual(self.session().model, config.MODEL_SMART)

    def test_commands_with_arguments_are_not_settings(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(text="/unknown thing"))
        answer.assert_called_once_with(60, "/unknown thing")

    def test_clear_wipes_history(self):
        self.session().remember("q", "a")
        self.app.handle_message(message(text="/clear"))
        self.assertEqual(self.session().history, [])
        self.assertEqual(self.texts()[-1], messages.HISTORY_CLEARED)

    def test_memory_off_wipes_history_too(self):
        s = self.session()
        s.memory_on = True
        s.remember("q", "a")
        self.app.handle_message(message(text="/memory_off"))
        self.assertEqual(s.history, [])

    def test_plain_text_reaches_the_pipeline(self):
        with mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(text="  سلام  "))
        answer.assert_called_once_with(60, "سلام")

    def test_bale_and_telegram_sessions_are_independent(self):
        self.app.handle_message(message(chat=77, text="/memory_on"))
        self.assertTrue(sessions.get(PLATFORM_BALE, 77).memory_on)
        self.assertFalse(sessions.get(PLATFORM_TELEGRAM, 77).memory_on)

    def test_tool_commands_use_the_session_model(self):
        self.session().model = config.MODEL_SMART
        with mock.patch.object(bh, "run_tool", return_value="TOOL OUT") as run:
            self.app.handle_message(message(text="/wiki Dijkstra"))
        self.assertEqual(run.call_args.args, ("wiki", "Dijkstra", config.MODEL_SMART))
        self.assertEqual(self.texts(), ["TOOL OUT"])

    def test_text_with_a_document_attached_is_not_treated_as_a_document(self):
        with mock.patch.object(self.app, "handle_document") as handle, mock.patch.object(self.app, "answer"):
            self.app.handle_message(message(text="hello", document={"file_id": "D"}))
        handle.assert_not_called()


class AnswerTests(BaleTestCase):
    def test_answer_runs_the_shared_pipeline_and_sends_the_result(self):
        with mock.patch.object(bh, "run_chat", return_value="ANS") as run:
            self.app.answer(60, "question", memory_text="mem")
        self.assertIs(run.call_args.args[0], self.session())
        self.assertEqual(run.call_args.kwargs["memory_text"], "mem")
        self.assertEqual(self.sent, [(60, "ANS")])


class QuizTests(BaleTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(quiz_service, "process_ai_query", return_value=(QUIZ_JSON, ""))
        p.start()
        self.addCleanup(p.stop)

    def test_quiz_flow_by_typing_letters(self):
        self.app.handle_message(message(text="/quiz toefl"))
        self.assertIn("B) widespread", self.texts()[-1])
        self.assertIn("حرف A تا D", self.texts()[-1])
        self.app.handle_message(message(text="b"))
        self.assertIn("✅ درست", self.texts()[-1])
        self.assertIn("/quiz", self.texts()[-1])  # tells the user how to continue
        with mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(text="b"))  # no active question any more -> normal chat
        answer.assert_called_once()

    def test_wrong_answer_and_stop(self):
        self.app.handle_message(message(text="/quiz"))
        self.app.handle_message(message(text="A"))
        self.assertIn("❌ نادرست", self.texts()[-1])
        self.app.handle_message(message(text="/quiz_stop"))
        self.assertIn("0 از 1", self.texts()[-1])

    def test_quiz_failure_is_reported(self):
        with mock.patch.object(quiz_service, "process_ai_query", side_effect=RuntimeError("down")):
            self.app.handle_message(message(text="/quiz"))
        self.assertIn("خطا در ساخت سؤال", self.texts()[-1])


class MediaTests(BaleTestCase):
    def test_pdf_document_with_caption(self):
        doc = {"file_id": "D1", "file_name": "jozve.pdf", "mime_type": "application/pdf", "file_size": 500}
        with mock.patch.object(self.app, "download", return_value=(b"%PDF-1.4", "documents/f.pdf")), \
                mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(caption="نکات مهم را بگو", document=doc))
        args, kwargs = answer.call_args
        self.assertEqual(args, (60, "نکات مهم را بگو"))
        self.assertEqual(kwargs["image"], {"data": b"%PDF-1.4", "mime": "application/pdf"})
        self.assertEqual(kwargs["memory_text"], "[PDF: jozve.pdf] نکات مهم را بگو")

    def test_pdf_without_caption_uses_default_prompt(self):
        doc = {"file_id": "D1", "file_name": "a.pdf"}
        with mock.patch.object(self.app, "download", return_value=(b"%PDF", "f.pdf")), mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(document=doc))
        self.assertEqual(answer.call_args.args[1], PDF_DEFAULT_PROMPT)

    def test_other_documents_are_refused(self):
        with mock.patch.object(self.app, "download") as download:
            self.app.handle_message(message(document={"file_id": "D", "file_name": "x.zip", "mime_type": "application/zip"}))
        download.assert_not_called()
        self.assertIn("فقط فایل PDF", self.texts()[0])

    def test_oversize_pdf_is_refused_before_and_after_download(self):
        doc = {"file_id": "D", "file_name": "a.pdf", "file_size": config.MAX_PDF_BYTES + 1}
        with mock.patch.object(self.app, "download") as download:
            self.app.handle_message(message(document=doc))
        download.assert_not_called()
        small_claim = {"file_id": "D", "file_name": "a.pdf", "file_size": 10}
        with mock.patch.object(self.app, "download", return_value=(b"x" * (config.MAX_PDF_BYTES + 1), "a.pdf")), \
                mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(document=small_claim))
        answer.assert_not_called()

    def test_unauthorized_documents_are_never_downloaded(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]), mock.patch.object(self.app, "download") as download:
            self.app.handle_message(message(user=999, document={"file_id": "D", "file_name": "a.pdf"}))
        download.assert_not_called()

    def test_photo_uses_the_largest_size_and_its_mime(self):
        photos = [
            {"file_id": "small", "file_size": 100, "width": 90, "height": 90},
            {"file_id": "large", "file_size": 9000, "width": 800, "height": 600},
        ]
        with mock.patch.object(self.app, "download", return_value=(b"PNG", "photos/a.png")) as download, \
                mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(photo=photos, caption="حل کن"))
        download.assert_called_once_with("large")
        self.assertEqual(answer.call_args.kwargs["image"]["mime"], "image/png")

    def test_photo_without_gemini_key(self):
        with mock.patch.object(clients, "gemini_client", None):
            self.app.handle_message(message(photo=[{"file_id": "x"}]))
        self.assertIn("GEMINI_API_KEY", self.texts()[0])

    def test_exam_mode_applies_to_documents(self):
        self.session().exam_mode = True
        with mock.patch.object(self.app, "download", return_value=(b"%PDF", "f.pdf")), mock.patch.object(self.app, "answer") as answer:
            self.app.handle_message(message(document={"file_id": "D", "file_name": "a.pdf"}))
        self.assertIn("RESULT", answer.call_args.args[1])


class TransportTests(BaseTestCase):
    """The real send/download methods (no patching of ``send`` here)."""

    def setUp(self):
        super().setUp()
        self.app = bh.BaleApp("TEST-BALE-TOKEN")

    def test_base_url_contains_the_token(self):
        self.assertEqual(self.app.base_url, f"{config.BALE_API_BASE}/botTEST-BALE-TOKEN")

    def test_long_messages_are_split_into_chunks(self):
        with mock.patch("requests.post") as post:
            self.app.send(1, "\n".join(f"line {i}" for i in range(2000)))
        self.assertGreater(post.call_count, 1)
        for call in post.call_args_list:
            self.assertLessEqual(len(call.kwargs["json"]["text"]), config.BALE_CHUNK_SIZE)
            self.assertEqual(call.kwargs["json"]["chat_id"], 1)
            self.assertTrue(call.args[0].endswith("/sendMessage"))

    def test_empty_text_gets_a_placeholder(self):
        with mock.patch("requests.post") as post:
            self.app.send(1, "  ")
        self.assertEqual(post.call_args.kwargs["json"]["text"], messages.NO_RESPONSE)

    def test_send_errors_are_swallowed(self):
        with mock.patch("requests.post", side_effect=RuntimeError("network")):
            self.app.send(1, "hello")  # must not raise

    def test_download_resolves_the_file_path_then_fetches_the_file(self):
        responses = [FakeResponse({"result": {"file_path": "voices/file_1.oga"}}), FakeResponse(content=b"AUDIO")]
        with mock.patch("requests.get", side_effect=responses) as get:
            data, path = self.app.download("FID")
        self.assertEqual((data, path), (b"AUDIO", "voices/file_1.oga"))
        self.assertEqual(get.call_args_list[0].kwargs["params"], {"file_id": "FID"})
        self.assertEqual(get.call_args_list[1].args[0], f"{config.BALE_API_BASE}/file/botTEST-BALE-TOKEN/voices/file_1.oga")

    def test_download_accepts_absolute_urls(self):
        responses = [FakeResponse({"result": {"file_path": "https://cdn.example/f.pdf"}}), FakeResponse(content=b"X")]
        with mock.patch("requests.get", side_effect=responses) as get:
            self.app.download("FID")
        self.assertEqual(get.call_args_list[1].args[0], "https://cdn.example/f.pdf")

    def test_download_without_file_path_fails_clearly(self):
        with mock.patch("requests.get", return_value=FakeResponse({"result": {}})):
            with self.assertRaises(RuntimeError):
                self.app.download("FID")


class PollingTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = bh.BaleApp("TEST-BALE-TOKEN")

    def updates(self, *ids):
        return FakeResponse({"result": [{"update_id": i, "message": {"chat": {"id": 1}, "text": f"m{i}"}} for i in ids]})

    def test_offset_advances_past_the_last_update(self):
        with mock.patch("requests.get", return_value=self.updates(10, 11)) as get, mock.patch.object(self.app, "handle_message") as handle:
            self.app.poll_once()
        self.assertEqual(self.app.offset, 12)
        self.assertEqual(handle.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"], {"offset": 0, "timeout": 12})
        with mock.patch("requests.get", return_value=self.updates()) as get:
            self.app.poll_once()
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 12)

    def test_a_failing_update_does_not_block_the_next_ones(self):
        calls = []

        def handler(msg):
            calls.append(msg["text"])
            if msg["text"] == "m1":
                raise RuntimeError("bad update")

        with mock.patch("requests.get", return_value=self.updates(1, 2)), mock.patch.object(self.app, "handle_message", handler):
            self.app.poll_once()
        self.assertEqual(calls, ["m1", "m2"])
        self.assertEqual(self.app.offset, 3)  # the poisoned update is skipped, not retried forever

    def test_non_200_response_is_ignored(self):
        with mock.patch("requests.get", return_value=FakeResponse({}, status=502)), mock.patch.object(self.app, "handle_message") as handle:
            self.app.poll_once()
        handle.assert_not_called()

    def test_run_survives_network_errors_and_keeps_polling(self):
        class StopLoop(BaseException):
            pass

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise StopLoop

        with mock.patch("requests.get"), \
                mock.patch.object(self.app, "poll_once", side_effect=[RuntimeError("net down"), None]) as poll, \
                mock.patch("time.sleep", sleep):
            with self.assertRaises(StopLoop):
                self.app.run()
        self.assertEqual(poll.call_count, 2)       # polled again after the failure
        self.assertEqual(sleeps, [2, 0.5])         # 2 s back-off after the error, 0.5 s on success
