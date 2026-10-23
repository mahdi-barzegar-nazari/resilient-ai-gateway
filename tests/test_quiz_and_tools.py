import json
import unittest
from unittest import mock

from core.errors import ToolError
from core.session import Session
from services import quiz_service as quiz
from services import tools

GOOD = json.dumps({
    "question": "Choose the synonym of 'ubiquitous'.",
    "options": ["A) rare", "B) widespread", "C) fragile", "D) ancient"],
    "answer": "b",
    "explanation": "ubiquitous یعنی همه‌جا حاضر.",
})


class QuizParsingTests(unittest.TestCase):
    def test_valid_json_is_normalised(self):
        q = quiz.parse_quiz_json("<think>{x}</think>```json\n" + GOOD + "\n```")
        self.assertEqual(q["options"], ["rare", "widespread", "fragile", "ancient"])
        self.assertEqual(q["answer"], "B")

    def test_option_text_starting_with_a_dash_is_not_mangled(self):
        data = json.loads(GOOD)
        data["options"][0] = "A-level"
        self.assertEqual(quiz.parse_quiz_json(json.dumps(data))["options"][0], "A-level")

    def test_invalid_structures_are_rejected(self):
        bad_payloads = [
            '{"question":"q","options":["a","b","c"],"answer":"A","explanation":"e"}',
            '{"question":"q","options":["a","b","c","d"],"answer":"E","explanation":"e"}',
            '{"question":"q","options":["a","b","c","d"],"answer":"","explanation":"e"}',
            '{"question":"q","options":["a","b","c","d"],"answer":"A","explanation":""}',
            "no json at all", "[1,2]",
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    quiz.parse_quiz_json(payload)

    def test_typed_answers(self):
        cases = {"a": "A", " B.": "B", "c)": "C", "4": "D", "hello": None, "AB": None, "": None, None: None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(quiz.parse_quiz_letter(text), expected)


class QuizFlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.outputs = [GOOD]

        def fake_process(prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0], ""

        p = mock.patch.object(quiz, "process_ai_query", fake_process)
        p.start()
        self.addCleanup(p.stop)

    def test_question_is_formatted_and_stored(self):
        s = Session()
        text, quiz_id = quiz.start_quiz(s, "toefl grammar", "HINT")
        self.assertEqual(quiz_id, 1)
        self.assertIn("A) rare", text)
        self.assertIn("B) widespread", text)
        self.assertTrue(text.endswith("HINT"))
        self.assertTrue(quiz.has_active_quiz(s))
        prompt, kwargs = self.calls[0]
        self.assertIn("toefl grammar", prompt)
        self.assertIs(kwargs["use_web"], False)
        self.assertEqual(kwargs["task_prompt"], quiz.QUIZ_SYSTEM_PROMPT)

    def test_invalid_json_is_retried_once(self):
        self.outputs = ["garbage", GOOD]
        text, quiz_id = quiz.start_quiz(Session(), "", "H")
        self.assertEqual(quiz_id, 1)
        self.assertEqual(len(self.calls), 2)

    def test_two_invalid_answers_give_up_gracefully(self):
        self.outputs = ["garbage"]
        text, quiz_id = quiz.start_quiz(Session(), "", "H")
        self.assertIsNone(quiz_id)
        self.assertIn("دوباره /quiz", text)

    def test_provider_failure_is_reported_without_state(self):
        with mock.patch.object(quiz, "process_ai_query", side_effect=RuntimeError("Groq | Gemini")):
            s = Session()
            text, quiz_id = quiz.start_quiz(s, "", "H")
        self.assertIsNone(quiz_id)
        self.assertIn("خطا در ساخت سؤال", text)
        self.assertFalse(quiz.has_active_quiz(s))

    def test_wrong_answer_reveals_the_right_option_and_scores(self):
        s = Session()
        quiz.start_quiz(s, "", "H")
        correct, text = quiz.grade_quiz(s, "A")
        self.assertFalse(correct)
        self.assertIn("پاسخ صحیح: B) widespread", text)
        self.assertIn("0 از 1", text)

    def test_a_question_can_be_answered_only_once(self):
        s = Session()
        quiz.start_quiz(s, "", "H")
        self.assertIsNotNone(quiz.grade_quiz(s, "B"))
        self.assertIsNone(quiz.grade_quiz(s, "B"))

    def test_stale_question_id_is_rejected_and_keeps_the_new_question(self):
        s = Session()
        _, first = quiz.start_quiz(s, "", "H")
        _, second = quiz.start_quiz(s, "", "H")
        self.assertNotEqual(first, second)
        self.assertIsNone(quiz.grade_quiz(s, "B", quiz_id=first))
        self.assertTrue(quiz.has_active_quiz(s))
        self.assertIsNotNone(quiz.grade_quiz(s, "B", quiz_id=second))

    def test_topic_is_remembered_and_prompt_adapts_to_score(self):
        s = Session()
        quiz.start_quiz(s, "toefl grammar", "H")
        quiz.grade_quiz(s, "B")
        text, _ = quiz.start_quiz(s, "", "H")
        self.assertIn("سؤال 2 — toefl grammar", text)
        prompt = self.calls[-1][0]
        self.assertIn("1 پاسخ درست از 1", prompt)
        self.assertIn("Choose the synonym", prompt)  # previous question is listed to avoid repeats

    def test_summary_resets_everything(self):
        s = Session()
        quiz.start_quiz(s, "", "H")
        quiz.grade_quiz(s, "A")
        quiz.start_quiz(s, "", "H")
        quiz.grade_quiz(s, "B")
        self.assertIn("(50٪)", quiz.quiz_summary(s))
        self.assertIsNone(s.quiz_stats)
        self.assertFalse(quiz.has_active_quiz(s))
        self.assertIn("آزمونی در جریان نبود", quiz.quiz_summary(s))

    def test_sessions_do_not_share_quizzes(self):
        a, b = Session(), Session()
        quiz.start_quiz(a, "", "H")
        self.assertFalse(quiz.has_active_quiz(b))


class ToolsTests(unittest.TestCase):
    def test_split_command(self):
        self.assertEqual(tools.split_command("/debug@mybot  NameError: x\nline2"), ("debug", "NameError: x\nline2"))
        self.assertEqual(tools.split_command("/QUIZ"), ("quiz", ""))
        self.assertEqual(tools.split_command(""), ("", ""))
        self.assertEqual(tools.split_command(None), ("", ""))

    def test_debug_uses_its_own_prompt_long_mode_and_no_web(self):
        with mock.patch.object(tools, "process_ai_query", return_value=("FIX", "")) as process:
            out = tools.run_tool("debug", "IndexError: list index", "some-model")
        self.assertEqual(out, "FIX")
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["task_prompt"], tools.DEBUG_SYSTEM_PROMPT)
        self.assertTrue(kwargs["is_long"])
        self.assertFalse(kwargs["use_web"])
        self.assertEqual(kwargs["model_name"], "some-model")

    def test_debug_without_argument_shows_usage(self):
        self.assertIn("مثال: /debug", tools.run_tool("debug", "", "m"))

    def test_tool_errors_become_text(self):
        with mock.patch.dict(tools.TOOLS, {"wiki": mock.Mock(side_effect=ToolError("friendly"))}):
            self.assertEqual(tools.run_tool("wiki", "x", "m"), "friendly")

    def test_unexpected_errors_are_summarised_and_never_raise(self):
        with mock.patch.dict(tools.TOOLS, {"wiki": mock.Mock(side_effect=ValueError("boom"))}):
            self.assertEqual(tools.run_tool("wiki", "x", "m"), "خطا: ValueError: boom")

    def test_registry_contents(self):
        self.assertEqual(set(tools.TOOLS), {"debug", "read", "wiki", "arxiv", "arxiv_full"})


if __name__ == "__main__":
    unittest.main()
