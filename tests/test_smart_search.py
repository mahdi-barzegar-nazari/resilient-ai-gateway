"""Smart web search end to end: planner -> search -> prompt -> answer -> sources, through the real pipeline, router and planner.

Only the network edges are fake: scripted Groq / Gemini clients (the planner call is recognised by its system prompt) and
``requests``. Both messengers run their real adapters, so the rendering and the per-user rate limit are checked where users
meet them. No network, no sleeping.
"""
from unittest import mock

import config
from bot import bale_handler as bh
from bot import messages
from bot import telegram_handler as th
from core import pipeline, ratelimit
from core.ratelimit import RateLimiter
from core.session import PLATFORM_BALE, PLATFORM_TELEGRAM, Session, sessions
from services import key_pool
from services import web_service as web
from services.key_pool import KeyPool
from services.search_planner import PLANNER_SYSTEM_PROMPT
from tests.helpers import (
    APITimeoutError, BaseTestCase, FakeClock, FakeGemini, FakeGroq, FakeResponse, install_providers, patch_bot, tg_message,
)

PLAN_DOLLAR = '{"search": true, "query": "قیمت دلار امروز"}'
PLAN_EURO = '{"search": true, "query": "قیمت یورو امروز"}'
PLAN_NO = '{"search": false, "query": ""}'
DOLLAR_QUESTION = "الان دلار چند شده؟"   # no keyword the old heuristic knows ('دلار' is one, so use the planner in the tests)
PLAIN_QUESTION = "الگوریتم مرتب‌سازی حبابی را توضیح بده"
SEARCH_LINKS = ["https://news.example/dollar?a=1&b=2", "https://rates.example/usd", "https://third.example/x"]


def is_planner_call(kwargs):
    if "messages" in kwargs:  # Groq
        return kwargs["messages"][0]["content"] == PLANNER_SYSTEM_PROMPT
    return kwargs["config"].system_instruction == PLANNER_SYSTEM_PROMPT  # Gemini


def tavily_payload(links=SEARCH_LINKS):
    return {
        "answer": "حدود ۶۰ هزار تومان",
        "results": [{"title": f"T{i}", "content": f"C{i}", "url": url} for i, url in enumerate(links, 1)],
    }


class SmartSearchTestCase(BaseTestCase):
    """Fake providers + fake Tavily; ``planner_reply`` / ``groq_answer`` / ``gemini_answer`` can be a text, an Exception or a callable."""

    def setUp(self):
        super().setUp()
        self.cache = web.TTLCache(clock=FakeClock())
        for patcher in (
            mock.patch.object(web, "search_cache", self.cache),
            mock.patch.object(config, "SEARCH_PLANNER_ENABLED", True),
            mock.patch.object(config, "SEARCH_PLANNER_TIMEOUT_SECONDS", 4.0),
            mock.patch.object(config, "SEARCH_PLANNER_HISTORY_MESSAGES", 4),
            mock.patch.object(config, "SEARCH_CACHE_TTL_SECONDS", 600.0),
            mock.patch.object(config, "SEARCH_CACHE_MAX_ENTRIES", 128),
            mock.patch.object(config, "SEARCH_MAX_SOURCES", 3),
            mock.patch("requests.post", side_effect=self.tavily_post),
            mock.patch("requests.get", side_effect=AssertionError("RSS must not be reached")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.planner_reply = PLAN_DOLLAR
        self.groq_answer, self.gemini_answer = "GROQ-ANSWER", "GEMINI-ANSWER"
        self.tavily_calls, self.tavily_data = [], tavily_payload()
        self.groq = FakeGroq(lambda kw: self.respond(kw, "planner_reply", "groq_answer"))
        self.gemini = FakeGemini(lambda kw: self.respond(kw, "planner_reply", "gemini_answer"))
        install_providers(self, self.groq, self.gemini)

    # -- fakes ---------------------------------------------------------------------------
    def respond(self, kwargs, planner_attr, answer_attr):
        outcome = getattr(self, planner_attr if is_planner_call(kwargs) else answer_attr)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(kwargs) if callable(outcome) else outcome

    def tavily_post(self, url, **kwargs):
        self.tavily_calls.append((url, kwargs))
        return FakeResponse(self.tavily_data)

    # -- views ---------------------------------------------------------------------------
    @staticmethod
    def planner_calls(fake):
        return [c for c in fake.calls if is_planner_call(c)]

    @staticmethod
    def answer_calls(fake):
        return [c for c in fake.calls if not is_planner_call(c)]

    def groq_prompt(self):
        """User text of the one real Groq answer call."""
        (call,) = self.answer_calls(self.groq)
        return call["messages"][-1]["content"]

    def gemini_prompt(self):
        (call,) = self.answer_calls(self.gemini)
        return call["contents"][-1].parts[0].text

    def searched_queries(self):
        return [kwargs["json"]["query"] for _, kwargs in self.tavily_calls]


class DefaultGroqPathTests(SmartSearchTestCase):
    def test_the_default_groq_model_now_searches_and_lists_sources(self):
        out = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertTrue(out.startswith("GROQ-ANSWER\n\nمنابع:\n1. https://news.example/dollar?a=1&b=2\n2. "))
        self.assertEqual(out.count("https://"), 3)
        self.assertIn("[داده‌های زنده وب]", self.groq_prompt())
        self.assertIn("C1", self.groq_prompt())
        self.assertEqual(self.searched_queries(), ["قیمت دلار امروز"])  # the planner's query, not the raw message
        self.assertEqual(self.gemini.calls, [])

    def test_the_planner_is_one_cheap_deterministic_call_on_the_fast_model(self):
        pipeline.run_chat(Session(model=config.MODEL_SMART), DOLLAR_QUESTION)
        (call,) = self.planner_calls(self.groq)
        self.assertEqual(call["model"], config.MODEL_FAST)  # not the user's selected model
        self.assertEqual((call["temperature"], call["timeout"]), (0, 4.0))

    def test_no_search_no_sources_when_the_planner_says_no(self):
        self.planner_reply = PLAN_NO
        out = pipeline.run_chat(Session(), "قیمت‌گذاری در اقتصاد خرد را توضیح بده")  # a keyword message the planner overrules
        self.assertEqual(out, "GROQ-ANSWER")
        self.assertEqual(self.tavily_calls, [])
        self.assertNotIn("زنده وب", self.groq_prompt())

    def test_a_search_without_any_result_answers_normally(self):
        self.tavily_data = {"results": []}
        with mock.patch("requests.get", return_value=FakeResponse(content=b"<rss><channel></channel></rss>")):
            out = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertEqual(out, "GROQ-ANSWER")


class GeminiAndFailoverTests(SmartSearchTestCase):
    def test_gemini_first_path_searches_and_lists_sources(self):
        out = pipeline.run_chat(Session(model=config.MODEL_GEMINI), DOLLAR_QUESTION)
        self.assertTrue(out.startswith("GEMINI-ANSWER\n\nمنابع:\n"))
        self.assertIn("C1", self.gemini_prompt())
        self.assertEqual(self.answer_calls(self.groq), [])
        self.assertEqual(len(self.tavily_calls), 1)

    def test_groq_failure_replays_the_same_enriched_prompt_on_gemini_with_one_search(self):
        self.groq_answer = RuntimeError("429 rate limited")
        out = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertTrue(out.startswith("GEMINI-ANSWER\n\nمنابع:\n"))
        self.assertTrue(out.endswith("↪️ (سرویس Groq پاسخ نداد؛ این پاسخ با Gemini تولید شد)"))
        self.assertLess(out.index("منابع:"), out.index("↪️"))  # sources first, the failover note last
        self.assertIn("C1", self.gemini_prompt())
        self.assertEqual(len(self.tavily_calls), 1)
        self.assertEqual(len(self.planner_calls(self.groq)), 1)

    def test_gemini_failure_replays_on_groq_with_one_search(self):
        self.gemini_answer = RuntimeError("503")
        out = pipeline.run_chat(Session(model=config.MODEL_GEMINI), DOLLAR_QUESTION)
        self.assertIn("GROQ-ANSWER", out)
        self.assertIn("منابع:", out)
        self.assertIn("C1", self.groq_prompt())
        self.assertEqual(len(self.tavily_calls), 1)

    def test_without_a_groq_key_gemini_plans_and_answers(self):
        install_providers(self, None, self.gemini)
        out = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertTrue(out.startswith("GEMINI-ANSWER\n\nمنابع:"))
        self.assertEqual(len(self.planner_calls(self.gemini)), 1)
        self.assertEqual(self.gemini.calls[0]["config"].http_options.timeout, 4000)

    def test_when_groq_fails_the_planner_asks_gemini_and_the_answer_still_arrives(self):
        self.groq = FakeGroq(RuntimeError("down"))  # planner and answer both fail on Groq
        install_providers(self, self.groq, self.gemini)
        out = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertTrue(out.startswith("GEMINI-ANSWER\n\nمنابع:"))
        self.assertEqual(len(self.planner_calls(self.gemini)), 1)
        self.assertEqual(self.searched_queries(), ["قیمت دلار امروز"])


class PlannerFailureNeverBlocksTests(SmartSearchTestCase):
    def test_every_planner_failure_still_gives_an_answer_via_the_keyword_heuristic(self):
        failures = {
            "provider error": RuntimeError("500"),
            "timeout": APITimeoutError("timed out"),
            "empty": "",
            "invalid json": "I would search for it",
            "wrong types": '{"search": "yes", "query": 1}',
        }
        for name, failure in failures.items():
            with self.subTest(failure=name):
                self.tavily_calls.clear()
                self.groq.calls.clear()
                self.gemini.calls.clear()
                self.planner_reply = failure
                out = pipeline.run_chat(Session(), "قیمت دلار امروز چنده؟")  # the old heuristic says: search
                self.assertTrue(out.startswith("GROQ-ANSWER") or out.startswith("GEMINI-ANSWER"), out)
                self.assertEqual(len(self.tavily_calls), 1)  # the old behaviour: raw cleaned message as the query
                self.assertEqual(self.searched_queries(), ["قیمت دلار امروز چنده"])
                self.cache.clear()

    def test_a_planner_failure_on_a_plain_question_means_no_search(self):
        self.planner_reply = RuntimeError("500")
        out = pipeline.run_chat(Session(), PLAIN_QUESTION)
        self.assertEqual(out, "GROQ-ANSWER")
        self.assertEqual(self.tavily_calls, [])

    def test_the_planner_can_be_switched_off(self):
        with mock.patch.object(config, "SEARCH_PLANNER_ENABLED", False):
            out = pipeline.run_chat(Session(), "قیمت دلار امروز چنده؟")
            plain = pipeline.run_chat(Session(), PLAIN_QUESTION)
        self.assertIn("منابع:", out)                  # the keyword heuristic, now on the Groq path too
        self.assertEqual(plain, "GROQ-ANSWER")
        self.assertEqual(self.planner_calls(self.groq) + self.planner_calls(self.gemini), [])

    def test_a_crash_inside_the_planner_never_reaches_the_user(self):
        with mock.patch("services.search_planner.ask_planner", side_effect=ValueError("bug")):
            out = pipeline.run_chat(Session(), "قیمت دلار امروز چنده؟")
        self.assertTrue(out.startswith("GROQ-ANSWER"))


class SkipRuleTests(SmartSearchTestCase):
    def test_greetings_and_short_messages_cost_nothing_extra(self):
        for text in ("سلام", "سلام!", "ok", "ممنون"):
            with self.subTest(text=text):
                self.assertEqual(pipeline.run_chat(Session(), text), "GROQ-ANSWER")
        self.assertEqual(self.planner_calls(self.groq), [])
        self.assertEqual(self.tavily_calls, [])

    def test_explicit_search_phrases_search_without_a_planner_call(self):
        self.planner_reply = PLAN_NO  # would refuse if it were asked
        out = pipeline.run_chat(Session(), "سرچ کن قیمت طلا")
        self.assertIn("منابع:", out)
        self.assertEqual(self.planner_calls(self.groq), [])
        self.assertEqual(self.searched_queries(), ["قیمت طلا"])


class FollowUpTests(SmartSearchTestCase):
    def test_the_planner_sees_recent_history_and_writes_a_self_contained_query(self):
        session = Session(memory_on=True)
        pipeline.run_chat(session, "قیمت دلار الان چنده؟")
        self.planner_reply = PLAN_EURO
        pipeline.run_chat(session, "و یورو؟")
        second_planner_prompt = self.planner_calls(self.groq)[-1]["messages"][1]["content"]
        self.assertIn("user: قیمت دلار الان چنده؟", second_planner_prompt)
        self.assertIn("assistant: GROQ-ANSWER", second_planner_prompt)
        self.assertTrue(second_planner_prompt.endswith("و یورو؟"))
        self.assertEqual(self.searched_queries(), ["قیمت دلار امروز", "قیمت یورو امروز"])

    def test_without_memory_there_is_no_history_to_show(self):
        session = Session(memory_on=False)
        pipeline.run_chat(session, "قیمت دلار الان چنده؟")
        pipeline.run_chat(session, "و یورو؟")
        self.assertNotIn("Recent messages", self.planner_calls(self.groq)[-1]["messages"][1]["content"])

    def test_stored_memory_never_contains_the_source_block(self):
        session = Session(memory_on=True)
        out = pipeline.run_chat(session, DOLLAR_QUESTION)
        self.assertIn("منابع:", out)
        self.assertEqual([m["content"] for m in session.history], [DOLLAR_QUESTION, "GROQ-ANSWER"])

    def test_a_bare_search_request_searches_the_previous_question(self):
        session = Session(memory_on=True)
        pipeline.run_chat(session, "نتیجه بازی پرسپولیس دیشب چی شد")
        self.tavily_calls.clear()
        self.planner_reply = PLAN_NO
        pipeline.run_chat(session, "سرچ کن")
        self.assertEqual(self.searched_queries(), ["نتیجه بازی پرسپولیس دیشب چی شد"])


class CacheTests(SmartSearchTestCase):
    def test_two_messages_with_the_same_query_search_once_and_both_show_sources(self):
        first = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        second = pipeline.run_chat(Session(), "دلار چنده الان؟")  # another wording, the planner writes the same query
        self.assertEqual(len(self.tavily_calls), 1)
        self.assertEqual(first.count("https://"), second.count("https://"))
        self.assertIn("منابع:", second)
        self.assertEqual(len(self.planner_calls(self.groq)), 2)  # the planner is not cached, only the search

    def test_the_cache_expires(self):
        clock = FakeClock()
        self.cache = web.TTLCache(clock=clock)
        with mock.patch.object(web, "search_cache", self.cache):
            pipeline.run_chat(Session(), DOLLAR_QUESTION)
            clock.advance(599)
            pipeline.run_chat(Session(), DOLLAR_QUESTION)
            self.assertEqual(len(self.tavily_calls), 1)
            clock.advance(1)
            pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertEqual(len(self.tavily_calls), 2)


class ExclusionTests(SmartSearchTestCase):
    def test_exam_mode_never_plans_searches_or_shows_sources(self):
        out = pipeline.run_chat(Session(exam_mode=True), DOLLAR_QUESTION)
        self.assertEqual(out, "GROQ-ANSWER")
        self.assertEqual((self.planner_calls(self.groq), self.tavily_calls), ([], []))
        out = pipeline.run_chat(Session(exam_mode=True, model=config.MODEL_GEMINI), DOLLAR_QUESTION)
        self.assertEqual(out, "GEMINI-ANSWER")
        self.assertEqual((self.planner_calls(self.groq), self.planner_calls(self.gemini), self.tavily_calls), ([], [], []))

    def test_exam_mode_failover_keeps_the_answer_sheet_line_clean(self):
        self.groq_answer = RuntimeError("429")
        out = pipeline.run_chat(Session(exam_mode=True), "Choose the correct word: He ___ to school today.")
        self.assertEqual(out, "GEMINI-ANSWER")  # no note, no sources

    def test_link_summary_prompts_never_plan_or_search(self):
        with mock.patch.object(pipeline, "prepare_link_prompt", return_value=("PAGE PROMPT", "[لینک] x", None)):
            out = pipeline.run_chat(Session(), "https://example.com/a")
        self.assertEqual(out, "GROQ-ANSWER")
        self.assertEqual((self.planner_calls(self.groq), self.tavily_calls), ([], []))

    def test_images_and_pdfs_never_plan_or_search(self):
        for mime in ("image/png", "application/pdf"):
            out = pipeline.run_chat(Session(), DOLLAR_QUESTION, image={"data": b"x", "mime": mime})
            self.assertEqual(out, "GEMINI-ANSWER")
        self.assertEqual((self.planner_calls(self.groq), self.planner_calls(self.gemini), self.tavily_calls), ([], [], []))

    def test_the_web_command_is_unchanged(self):
        app = th.TelegramApp(config.TELEGRAM_BOT_TOKEN)
        rec = patch_bot(self, app)
        app.on_web(tg_message(text="/web قیمت دلار؟"))
        self.assertEqual(self.planner_calls(self.groq) + self.planner_calls(self.gemini), [])
        self.assertEqual(self.searched_queries(), ["قیمت دلار"])            # filler stripped, exactly as before
        self.assertNotIn("منابع", rec.texts("reply_to")[0])
        self.assertEqual(len(self.cache), 0)                                  # and never cached
        app.on_web(tg_message(text="/web قیمت دلار؟"))
        self.assertEqual(len(self.tavily_calls), 2)


class TavilyKeyRotationTests(SmartSearchTestCase):
    def test_the_automatic_search_rotates_tavily_keys_like_every_other_search(self):
        clock = FakeClock()
        posted = []

        def post(url, **kwargs):
            key = kwargs["json"]["api_key"]
            posted.append(key)
            return FakeResponse({}, status=429) if key == "tav-1" else FakeResponse(tavily_payload())

        with mock.patch.object(config, "TAVILY_API_KEY", None), \
                mock.patch.object(config, "TAVILY_API_KEYS", ["tav-1", "tav-2"]), \
                mock.patch.object(web, "tavily_pool", KeyPool("tavily", lambda: config.api_keys("tavily"), clock=clock)), \
                mock.patch.object(key_pool, "clock", clock), \
                mock.patch("requests.post", side_effect=post):
            out = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        self.assertEqual(posted, ["tav-1", "tav-2"])
        self.assertIn("منابع:", out)


# ---------------------------------------------------------------- the messengers
class TelegramRenderingTests(SmartSearchTestCase):
    def setUp(self):
        super().setUp()
        self.app = th.TelegramApp(config.TELEGRAM_BOT_TOKEN)
        self.rec = patch_bot(self, self.app)

    def sent(self):
        return self.rec.texts("reply_to") + self.rec.texts("send_message")

    def test_the_answer_and_sources_arrive_as_telegram_html(self):
        self.app.on_text(tg_message(text=DOLLAR_QUESTION))
        (text,) = self.sent()
        self.assertEqual(self.rec.of("reply_to")[0][2]["parse_mode"], "HTML")
        self.assertTrue(text.startswith("GROQ-ANSWER\n\nمنابع:\n1. https://news.example/dollar?a=1&amp;b=2\n"))  # one escape
        self.assertNotIn("&amp;amp;", text)
        self.assertNotIn("<a ", text)  # plain links: Telegram makes them tappable itself

    def test_markdown_lookalikes_in_a_link_cannot_corrupt_the_message(self):
        self.tavily_data = tavily_payload(["https://x.example/**bold**/`code`/a b", "https://y.example/ok"])
        self.app.on_text(tg_message(text=DOLLAR_QUESTION))
        (text,) = self.sent()
        self.assertNotIn("<b>", text)
        self.assertNotIn("<code>", text)
        self.assertIn("https://x.example/%2A%2Abold%2A%2A/%60code%60/a%20b", text)

    def test_the_sources_survive_chunking_of_a_long_answer(self):
        self.groq_answer = ("سطر طولانی پاسخ. " * 30 + "\n") * 12  # well over one chunk
        self.app.on_text(tg_message(text=DOLLAR_QUESTION))
        texts = self.sent()
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) < 4096 for t in texts))
        self.assertIn("منابع:", texts[-1])
        self.assertEqual(texts[-1].count("https://"), 3)

    def test_exam_mode_shows_no_sources(self):
        sessions.get(PLATFORM_TELEGRAM, 1).exam_mode = True
        self.app.on_text(tg_message(text=DOLLAR_QUESTION))
        self.assertEqual(self.sent(), ["GROQ-ANSWER"])


class BaleRenderingTests(SmartSearchTestCase):
    def setUp(self):
        super().setUp()
        self.app = bh.BaleApp(config.BALE_BOT_TOKEN)
        self.sent = []
        patcher = mock.patch.object(self.app, "send", lambda chat_id, text: self.sent.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)

    def say(self, text, chat=60, user=111):
        self.app.handle_message({"chat": {"id": chat}, "from": {"id": user}, "text": text})

    def test_the_answer_and_sources_arrive_as_plain_text(self):
        self.say(DOLLAR_QUESTION)
        (text,) = self.sent
        self.assertEqual(text.splitlines()[0], "GROQ-ANSWER")
        self.assertIn("\n\nمنابع:\n1. https://news.example/dollar?a=1&b=2\n2. https://rates.example/usd\n3. https://third.example/x", text)
        self.assertNotIn("&amp;", text)  # Bale has no HTML mode: nothing may be escaped
        self.assertNotIn("<", text)

    def test_markdown_lookalikes_are_encoded_here_too(self):
        self.tavily_data = tavily_payload(["https://x.example/**bold**/`code`"])
        self.say(DOLLAR_QUESTION)
        self.assertIn("https://x.example/%2A%2Abold%2A%2A/%60code%60", self.sent[0])

    def test_the_real_transport_sends_the_text_unmodified(self):
        app = bh.BaleApp(config.BALE_BOT_TOKEN)
        reply = pipeline.run_chat(Session(), DOLLAR_QUESTION)
        with mock.patch("requests.post") as post:
            app.send(60, reply)
        self.assertEqual(post.call_args.kwargs["json"]["text"], reply)

    def test_exam_mode_shows_no_sources(self):
        sessions.get(PLATFORM_BALE, 60).exam_mode = True
        self.say(DOLLAR_QUESTION)
        self.assertEqual(self.sent, ["GROQ-ANSWER"])


# ---------------------------------------------------------------- the per-user rate limit
class PlannerIsNotAnExtraRequestTests(SmartSearchTestCase):
    """The planner (and the search) belong to the user's request: with a limit of 2, two planned messages both pass."""

    LIMIT = 2

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.limiter = RateLimiter(clock=self.clock)
        for patcher in (
            mock.patch.object(ratelimit, "limiter", self.limiter),
            mock.patch.object(config, "RATE_LIMIT_MAX", self.LIMIT),
            mock.patch.object(config, "RATE_LIMIT_WINDOW_SECONDS", 60.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def check_calls(self):
        return mock.patch.object(self.limiter, "check", wraps=self.limiter.check)

    def assert_two_pass_and_third_is_limited(self, say, sent_texts):
        with self.check_calls() as check:
            for _ in range(self.LIMIT):
                say()
            self.assertEqual(check.call_count, self.LIMIT)  # one check per message, none for the planner
        self.assertEqual(len(self.planner_calls(self.groq)), self.LIMIT)  # ...although the planner did run every time
        self.assertEqual(len(self.tavily_calls), 1)                         # (the second search came from the cache)
        answers = [t for t in sent_texts() if t.startswith("GROQ-ANSWER")]
        self.assertEqual(len(answers), self.LIMIT)
        say()
        self.assertEqual(len(self.planner_calls(self.groq)), self.LIMIT)  # the third message never reached the planner
        self.assertTrue(any(t.startswith("⏳") for t in sent_texts()))

    def test_telegram(self):
        app = th.TelegramApp(config.TELEGRAM_BOT_TOKEN)
        rec = patch_bot(self, app)
        self.assert_two_pass_and_third_is_limited(
            lambda: app.on_text(tg_message(user=111, text=DOLLAR_QUESTION)),
            lambda: rec.texts("reply_to") + rec.texts("send_message"),
        )

    def test_bale(self):
        app = bh.BaleApp(config.BALE_BOT_TOKEN)
        sent = []
        patcher = mock.patch.object(app, "send", lambda chat_id, text: sent.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assert_two_pass_and_third_is_limited(
            lambda: app.handle_message({"chat": {"id": 60}, "from": {"id": 111}, "text": DOLLAR_QUESTION}),
            lambda: sent,
        )

    def test_the_notice_text_is_the_normal_one(self):
        app = bh.BaleApp(config.BALE_BOT_TOKEN)
        sent = []
        patcher = mock.patch.object(app, "send", lambda chat_id, text: sent.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        for _ in range(self.LIMIT + 1):
            app.handle_message({"chat": {"id": 60}, "from": {"id": 111}, "text": DOLLAR_QUESTION})
        self.assertEqual(sent[-1], messages.rate_limited_text(60))
