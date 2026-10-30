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



class WebSourcesTests(unittest.TestCase):
    """Source links under the answer: label, order, memory and exam mode."""

    LINKS = ["https://a.example/1", "https://b.example/2", "https://c.example/3"]

    def setUp(self):
        self.kwargs = []
        self.links = list(self.LINKS)
        self.note = ""

        def fake_process(user_text, web_sources=None, **kwargs):
            self.kwargs.append({"web_sources": web_sources, **kwargs})
            web_sources.extend(self.links)  # what the router does when it searched
            return "ANSWER", self.note

        for patcher in (
            mock.patch.object(pipeline, "process_ai_query", fake_process),
            mock.patch.object(pipeline, "prepare_link_prompt", return_value=(None, None, None)),
            mock.patch.object(config, "SEARCH_MAX_SOURCES", 3),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sources_are_appended_under_the_answer_with_the_persian_label(self):
        out = pipeline.run_chat(Session(), "q")
        self.assertEqual(out, "ANSWER\n\nمنابع:\n1. https://a.example/1\n2. https://b.example/2\n3. https://c.example/3")

    def test_a_fresh_list_is_given_to_the_router_on_every_turn(self):
        pipeline.run_chat(Session(), "q")
        pipeline.run_chat(Session(), "q")
        first, second = (kw["web_sources"] for kw in self.kwargs)
        self.assertIsNot(first, second)
        self.assertEqual(second, self.LINKS)  # not doubled up by the previous turn

    def test_no_links_means_no_label(self):
        self.links = []
        self.assertEqual(pipeline.run_chat(Session(), "q"), "ANSWER")

    def test_the_failover_note_comes_after_the_sources(self):
        self.note = "\n\n↪️ (note)"
        out = pipeline.run_chat(Session(), "q")
        self.assertTrue(out.endswith("https://c.example/3\n\n↪️ (note)"))
        self.assertLess(out.index("منابع:"), out.index("↪️"))

    def test_memory_keeps_the_answer_without_its_sources(self):
        s = Session(memory_on=True)
        out = pipeline.run_chat(s, "q")
        self.assertIn("منابع:", out)
        self.assertEqual(s.history, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "ANSWER"}])

    def test_exam_mode_never_shows_sources(self):
        out = pipeline.run_chat(Session(exam_mode=True), "q")
        self.assertEqual(out, "ANSWER")
        self.assertNotIn("منابع", out)

    def test_at_most_the_configured_number_of_links_is_shown(self):
        self.links = [f"https://x.example/{i}" for i in range(6)]
        self.assertEqual(pipeline.run_chat(Session(), "q").count("https://"), 3)
        with mock.patch.object(config, "SEARCH_MAX_SOURCES", 2):
            self.assertEqual(pipeline.run_chat(Session(), "q").count("https://"), 2)

    def test_a_failure_before_the_answer_shows_no_sources(self):
        with mock.patch.object(pipeline, "process_ai_query", side_effect=RuntimeError("down")):
            out = pipeline.run_chat(Session(), "q")
        self.assertTrue(out.startswith("خطا در پردازش:"))
        self.assertNotIn("منابع", out)


if __name__ == "__main__":
    unittest.main()
