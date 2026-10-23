import unittest
from types import SimpleNamespace
from unittest import mock

import config
from core.prompts import FAILOVER_NOTE_TO_GEMINI, FAILOVER_NOTE_TO_GROQ
from services import ai_router as router
from services import clients


def groq_reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class RouterTestCase(unittest.TestCase):
    """Replaces both provider calls with controllable fakes."""

    def setUp(self):
        self.groq_calls, self.gemini_calls = [], []
        self.groq_behavior = lambda *a, **k: "GROQ-ANSWER"
        self.gemini_behavior = lambda *a, **k: "GEMINI-ANSWER"

        def fake_groq(user_text, sys_prompt, model_name, history, is_exam=False, is_long=False):
            self.groq_calls.append({"text": user_text, "sys": sys_prompt, "model": model_name,
                                    "history": history, "is_exam": is_exam, "is_long": is_long})
            return self.groq_behavior()

        def fake_gemini(user_text, sys_prompt, history, is_exam=False, is_long=False, image=None):
            self.gemini_calls.append({"text": user_text, "sys": sys_prompt, "history": history, "image": image})
            return self.gemini_behavior()

        for target, fake in (("call_groq", fake_groq), ("call_gemini", fake_gemini)):
            p = mock.patch.object(router, target, fake)
            p.start()
            self.addCleanup(p.stop)
        for client in ("groq_client", "gemini_client", "gemini_pdf_client"):
            p = mock.patch.object(clients, client, object())
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def failing(message="429 rate limited"):
        def _raise(*args, **kwargs):
            raise RuntimeError(message)
        return _raise


class FailoverTests(RouterTestCase):
    def test_groq_is_used_first_for_groq_models(self):
        answer, note = router.process_ai_query("سلام", model_name=config.MODEL_FAST)
        self.assertEqual((answer, note), ("GROQ-ANSWER", ""))
        self.assertEqual(self.gemini_calls, [])

    def test_groq_failure_falls_back_to_gemini_with_note(self):
        self.groq_behavior = self.failing()
        answer, note = router.process_ai_query("سلام", model_name=config.MODEL_FAST)
        self.assertEqual(answer, "GEMINI-ANSWER")
        self.assertEqual(note, FAILOVER_NOTE_TO_GEMINI)

    def test_gemini_failure_falls_back_to_groq_with_note(self):
        self.gemini_behavior = self.failing("timeout")
        answer, note = router.process_ai_query("سلام", model_name=config.MODEL_GEMINI)
        self.assertEqual(answer, "GROQ-ANSWER")
        self.assertEqual(note, FAILOVER_NOTE_TO_GROQ)

    def test_gemini_model_falls_back_to_the_fast_groq_model(self):
        self.gemini_behavior = self.failing()
        router.process_ai_query("سلام", model_name=config.MODEL_GEMINI)
        self.assertEqual(self.groq_calls[0]["model"], config.MODEL_FAST)

    def test_smart_model_is_preserved_on_groq(self):
        router.process_ai_query("سلام", model_name=config.MODEL_SMART)
        self.assertEqual(self.groq_calls[0]["model"], config.MODEL_SMART)

    def test_exam_mode_suppresses_the_failover_note(self):
        self.groq_behavior = self.failing()
        answer, note = router.process_ai_query("q", model_name=config.MODEL_FAST, is_exam=True)
        self.assertEqual((answer, note), ("GEMINI-ANSWER", ""))

    def test_both_providers_failing_raises_a_combined_error(self):
        self.groq_behavior = self.failing("429")
        self.gemini_behavior = self.failing("503")
        with self.assertRaises(RuntimeError) as ctx:
            router.process_ai_query("q", model_name=config.MODEL_FAST)
        message = str(ctx.exception)
        self.assertIn("Groq", message)
        self.assertIn("Gemini", message)
        self.assertIn("429", message)
        self.assertIn("503", message)

    def test_without_gemini_a_groq_failure_propagates(self):
        self.groq_behavior = self.failing()
        with mock.patch.object(clients, "gemini_client", None):
            with self.assertRaises(RuntimeError):
                router.process_ai_query("q", model_name=config.MODEL_FAST)

    def test_without_groq_gemini_is_used_directly(self):
        with mock.patch.object(clients, "groq_client", None):
            answer, note = router.process_ai_query("q", model_name=config.MODEL_FAST)
        self.assertEqual((answer, note), ("GEMINI-ANSWER", ""))

    def test_no_providers_configured(self):
        with mock.patch.object(clients, "groq_client", None), mock.patch.object(clients, "gemini_client", None):
            answer, note = router.process_ai_query("q")
        self.assertIn("پیکربندی نشده", answer)


class MultimodalTests(RouterTestCase):
    def test_image_goes_straight_to_gemini_without_failover(self):
        image = {"data": b"\xff", "mime": "image/jpeg"}
        answer, note = router.process_ai_query("what is this", image=image)
        self.assertEqual((answer, note), ("GEMINI-ANSWER", ""))
        self.assertIs(self.gemini_calls[0]["image"], image)
        self.assertEqual(self.groq_calls, [])

    def test_image_failure_is_not_retried_on_groq(self):
        self.gemini_behavior = self.failing()
        with self.assertRaises(RuntimeError):
            router.process_ai_query("q", image={"data": b"x", "mime": "application/pdf"})
        self.assertEqual(self.groq_calls, [])

    def test_image_without_gemini_key_explains(self):
        with mock.patch.object(clients, "gemini_client", None):
            answer, _ = router.process_ai_query("q", image={"data": b"x", "mime": "image/png"})
        self.assertIn("GEMINI_API_KEY", answer)


class PromptAssemblyTests(RouterTestCase):
    def test_task_prompt_and_long_prompt_reach_the_system_prompt(self):
        router.process_ai_query("q", is_long=True, task_prompt="TASK-MARKER")
        sys_prompt = self.groq_calls[0]["sys"]
        self.assertIn("TASK-MARKER", sys_prompt)
        self.assertIn("بدون خلاصه کردن", sys_prompt)

    def test_exam_prompt_wins_over_long(self):
        router.process_ai_query("q", is_exam=True, is_long=True)
        sys_prompt = self.groq_calls[0]["sys"]
        self.assertIn("Autonomous English Exam Protocol", sys_prompt)
        self.assertNotIn("بدون خلاصه کردن", sys_prompt)

    def test_default_prompt_has_no_task_section(self):
        router.process_ai_query("q")
        self.assertNotIn("TASK-MARKER", self.groq_calls[0]["sys"])

    def test_history_only_sent_when_memory_is_on_and_not_exam(self):
        history = [{"role": "user", "content": "old"}]
        router.process_ai_query("q", use_memory=True, history_list=history)
        self.assertEqual(self.groq_calls[-1]["history"], history)
        router.process_ai_query("q", use_memory=False, history_list=history)
        self.assertEqual(self.groq_calls[-1]["history"], [])
        router.process_ai_query("q", use_memory=True, history_list=history, is_exam=True)
        self.assertEqual(self.groq_calls[-1]["history"], [])


class WebEnrichmentTests(RouterTestCase):
    def test_web_context_added_only_on_the_gemini_path(self):
        with mock.patch.object(router, "fetch_web_context", return_value="WEBDATA") as fetch:
            router.process_ai_query("قیمت دلار امروز؟", model_name=config.MODEL_FAST)
            self.assertEqual(self.groq_calls[0]["text"], "قیمت دلار امروز؟")  # Groq gets the raw text
            fetch.assert_not_called()
            router.process_ai_query("قیمت دلار امروز؟", model_name=config.MODEL_GEMINI)
            self.assertIn("WEBDATA", self.gemini_calls[0]["text"])

    def test_web_context_is_added_when_failing_over_to_gemini(self):
        self.groq_behavior = self.failing()
        with mock.patch.object(router, "fetch_web_context", return_value="WEBDATA"):
            router.process_ai_query("قیمت دلار امروز؟", model_name=config.MODEL_FAST)
        self.assertIn("WEBDATA", self.gemini_calls[0]["text"])

    def test_use_web_false_disables_enrichment(self):
        with mock.patch.object(router, "fetch_web_context", return_value="WEBDATA") as fetch:
            router.process_ai_query("قیمت دلار امروز؟", model_name=config.MODEL_GEMINI, use_web=False)
        fetch.assert_not_called()

    def test_exam_mode_never_searches(self):
        with mock.patch.object(router, "fetch_web_context", return_value="WEBDATA") as fetch:
            router.process_ai_query("قیمت دلار امروز؟", model_name=config.MODEL_GEMINI, is_exam=True)
        fetch.assert_not_called()


class ProviderCallTests(unittest.TestCase):
    """The thin wrappers around the SDK calls (real call_groq / call_gemini, fake clients)."""

    def test_groq_token_budget_per_mode(self):
        create = mock.Mock(return_value=groq_reply("ok"))
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(clients, "groq_client", fake):
            router.call_groq("q", "sys", config.MODEL_FAST, [])
            self.assertEqual(create.call_args.kwargs["max_tokens"], 800)
            router.call_groq("q", "sys", config.MODEL_SMART, [])
            self.assertEqual(create.call_args.kwargs["max_tokens"], 1500)
            router.call_groq("q", "sys", config.MODEL_FAST, [], is_long=True)
            self.assertEqual(create.call_args.kwargs["max_tokens"], config.LONG_MODE_MAX_TOKENS)
            router.call_groq("q", "sys", config.MODEL_FAST, [], is_exam=True)
            self.assertEqual(create.call_args.kwargs["max_tokens"], 300)

    def test_groq_empty_answer_raises_so_failover_can_trigger(self):
        create = mock.Mock(return_value=groq_reply("   "))
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(clients, "groq_client", fake):
            with self.assertRaises(RuntimeError):
                router.call_groq("q", "sys", config.MODEL_FAST, [])

    def test_gemini_empty_answer_raises(self):
        generate = mock.Mock(return_value=SimpleNamespace(text=""))
        fake = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with mock.patch.object(clients, "gemini_client", fake), mock.patch.object(clients, "gemini_pdf_client", fake):
            with self.assertRaises(RuntimeError):
                router.call_gemini("q", "sys", [])

    def test_pdfs_use_the_long_timeout_client(self):
        used = []

        def make(name):
            return SimpleNamespace(models=SimpleNamespace(
                generate_content=lambda **kw: used.append(name) or SimpleNamespace(text="x")))

        with mock.patch.object(clients, "gemini_client", make("normal")), \
                mock.patch.object(clients, "gemini_pdf_client", make("pdf")):
            router.call_gemini("q", "sys", [], image={"data": b"%PDF", "mime": "application/pdf"})
            router.call_gemini("q", "sys", [], image={"data": b"\xff", "mime": "image/jpeg"})
            router.call_gemini("q", "sys", [])
        self.assertEqual(used, ["pdf", "normal", "normal"])

    def test_missing_client_raises_a_clear_error(self):
        with mock.patch.object(clients, "gemini_client", None):
            with self.assertRaises(RuntimeError):
                router.call_gemini("q", "sys", [])
        with mock.patch.object(clients, "groq_client", None):
            with self.assertRaises(RuntimeError):
                router.call_groq("q", "sys", config.MODEL_FAST, [])


class QuickWebAnswerTests(unittest.TestCase):
    def test_context_is_embedded_in_the_prompt(self):
        with mock.patch.object(router, "fetch_web_context", return_value="DATA"), \
                mock.patch.object(router, "call_gemini", return_value="ANS") as call:
            self.assertEqual(router.quick_web_answer("قیمت طلا"), "ANS")
        self.assertIn("DATA", call.call_args.args[0])

    def test_without_context_the_raw_query_is_used(self):
        with mock.patch.object(router, "fetch_web_context", return_value=""), \
                mock.patch.object(router, "call_gemini", return_value="ANS") as call:
            router.quick_web_answer("hello")
        self.assertEqual(call.call_args.args[0], "hello")


if __name__ == "__main__":
    unittest.main()
