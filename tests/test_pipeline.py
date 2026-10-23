import unittest
from unittest import mock

import config
from core import pipeline
from core.session import Session
from services.web_service import LINK_ERROR_TEXT


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_process(user_text, **kwargs):
            self.calls.append((user_text, kwargs))
            return "ANSWER", ""

        p = mock.patch.object(pipeline, "process_ai_query", fake_process)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(pipeline, "prepare_link_prompt", return_value=(None, None, None))
        self.prepare = q.start()
        self.addCleanup(q.stop)

    def test_plain_message_passes_session_settings_to_the_router(self):
        s = Session(model=config.MODEL_SMART, exam_mode=False, long_mode=True)
        self.assertEqual(pipeline.run_chat(s, "سلام"), "ANSWER")
        text, kw = self.calls[0]
        self.assertEqual(text, "سلام")
        self.assertEqual(kw["model_name"], config.MODEL_SMART)
        self.assertTrue(kw["is_long"])
        self.assertTrue(kw["use_web"])
        self.assertFalse(kw["use_memory"])

    def test_failover_note_is_appended(self):
        with mock.patch.object(pipeline, "process_ai_query", return_value=("A", "\n[note]")):
            self.assertEqual(pipeline.run_chat(Session(), "q"), "A\n[note]")

    def test_memory_off_stores_nothing(self):
        s = Session()
        pipeline.run_chat(s, "q")
        self.assertEqual(s.history, [])

    def test_memory_on_stores_the_exchange(self):
        s = Session(memory_on=True)
        pipeline.run_chat(s, "q")
        self.assertEqual(s.history, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "ANSWER"}])

    def test_history_is_sent_back_on_the_next_turn(self):
        s = Session(memory_on=True)
        pipeline.run_chat(s, "first")
        pipeline.run_chat(s, "second")
        self.assertEqual(len(self.calls[1][1]["history_list"]), 2)

    def test_exam_mode_never_stores_memory(self):
        s = Session(memory_on=True, exam_mode=True)
        pipeline.run_chat(s, "q")
        self.assertEqual(s.history, [])

    def test_link_prompt_replaces_the_text_and_disables_web_search(self):
        self.prepare.return_value = ("PROMPT-WITH-PAGE", "[لینک] short", None)
        s = Session(memory_on=True)
        pipeline.run_chat(s, "https://example.com")
        text, kw = self.calls[0]
        self.assertEqual(text, "PROMPT-WITH-PAGE")
        self.assertFalse(kw["use_web"])
        # memory keeps the short note, never the (large) page prompt
        self.assertEqual(s.history[0]["content"], "[لینک] short")

    def test_link_error_is_returned_without_calling_the_model(self):
        self.prepare.return_value = (None, None, LINK_ERROR_TEXT)
        self.assertEqual(pipeline.run_chat(Session(), "https://example.com"), LINK_ERROR_TEXT)
        self.assertEqual(self.calls, [])

    def test_exam_mode_skips_link_handling(self):
        pipeline.run_chat(Session(exam_mode=True), "https://example.com")
        self.prepare.assert_not_called()

    def test_media_input_skips_link_handling_and_keeps_its_memory_text(self):
        s = Session(memory_on=True)
        image = {"data": b"x", "mime": "image/png"}
        pipeline.run_chat(s, "https://example.com in a caption", image=image, memory_text="[تصویر] cap")
        self.prepare.assert_not_called()
        self.assertEqual(self.calls[0][1]["image"], image)
        self.assertEqual(s.history[0]["content"], "[تصویر] cap")

    def test_errors_become_readable_redacted_text_and_nothing_is_remembered(self):
        s = Session(memory_on=True)
        boom = RuntimeError(f"Groq failed with key {config.GROQ_API_KEY}")
        with mock.patch.object(pipeline, "process_ai_query", side_effect=boom):
            out = pipeline.run_chat(s, "q")
        self.assertTrue(out.startswith("خطا در پردازش:"))
        self.assertNotIn(config.GROQ_API_KEY, out)
        self.assertEqual(s.history, [])


if __name__ == "__main__":
    unittest.main()
