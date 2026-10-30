import threading
import unittest
from unittest import mock

import config
from core.errors import ToolError
from services import web_service as web
from tests.helpers import FakeClock, FakeResponse
from utils.formatters import to_telegram_html


class SearchHeuristicTests(unittest.TestCase):
    def test_explicit_search_phrases_trigger(self):
        self.assertTrue(web.should_search_web("لطفا سرچ کن آب و هوای تهران"))
        self.assertTrue(web.should_search_web("search python 3.13 release"))

    def test_freshness_keywords_trigger(self):
        self.assertTrue(web.should_search_web("قیمت دلار امروز چنده"))

    def test_greetings_and_tiny_messages_do_not(self):
        self.assertFalse(web.should_search_web("سلام"))
        self.assertFalse(web.should_search_web("ok"))

    def test_plain_question_does_not(self):
        self.assertFalse(web.should_search_web("الگوریتم مرتب‌سازی حبابی را توضیح بده"))

    def test_query_cleaning_strips_filler_words(self):
        self.assertEqual(web.clean_query_for_search("سرچ کن قیمت طلا؟"), "قیمت طلا")
        self.assertEqual(web.clean_query_for_search("سرچ"), "سرچ")  # never returns an empty query


class FetchWebContextTests(unittest.TestCase):
    def test_tavily_results_are_formatted(self):
        payload = {"answer": "42", "results": [{"title": "T", "content": "C"}]}
        with mock.patch("requests.post", return_value=FakeResponse(payload)) as post:
            out = web.fetch_web_context("قیمت دلار")
        self.assertIn("• پاسخ سریع: 42", out)
        self.assertIn("• T: C", out)
        self.assertEqual(post.call_args.kwargs["json"]["api_key"], config.TAVILY_API_KEY)

    def test_falls_back_to_rss_when_tavily_fails(self):
        rss = b"<rss><channel><item><title>Headline 1</title></item><item><title>Headline 2</title></item></channel></rss>"
        with mock.patch("requests.post", side_effect=RuntimeError("down")), \
                mock.patch("requests.get", return_value=FakeResponse(content=rss)):
            out = web.fetch_web_context("خبر")
        self.assertEqual(out, "• خبر: Headline 1\n• خبر: Headline 2")

    def test_rss_is_used_without_tavily_key(self):
        rss = b"<rss><channel><item><title>Only</title></item></channel></rss>"
        with mock.patch.object(config, "TAVILY_API_KEY", None), \
                mock.patch("requests.post") as post, \
                mock.patch("requests.get", return_value=FakeResponse(content=rss)):
            out = web.fetch_web_context("خبر")
        post.assert_not_called()
        self.assertIn("Only", out)

    def test_total_failure_returns_empty_string(self):
        with mock.patch("requests.post", side_effect=RuntimeError("x")), \
                mock.patch("requests.get", side_effect=RuntimeError("y")):
            self.assertEqual(web.fetch_web_context("خبر"), "")


class UrlExtractionTests(unittest.TestCase):
    def test_trailing_punctuation_is_stripped(self):
        self.assertEqual(web.extract_urls("ببین https://example.com/a?b=1."), ["https://example.com/a?b=1"])
        self.assertEqual(web.extract_urls("(https://example.com/x)،"), ["https://example.com/x"])

    def test_balanced_parenthesis_is_kept(self):
        url = "https://en.wikipedia.org/wiki/Foo_(bar)"
        self.assertEqual(web.extract_urls(f"({url})"), [url])

    def test_duplicates_removed_order_kept(self):
        self.assertEqual(
            web.extract_urls("https://a.com/x https://a.com/x http://b.org"),
            ["https://a.com/x", "http://b.org"],
        )

    def test_no_links(self):
        self.assertEqual(web.extract_urls("no link here"), [])
        self.assertEqual(web.extract_urls(None), [])


class CleanPageTextTests(unittest.TestCase):
    def test_markdown_noise_removed(self):
        raw = "![img](http://x/y.png)\n\n\n\nمتن [لینک](http://z) و http://raw.com   تمام"
        out = web.clean_page_text(raw)
        self.assertNotIn("http", out)
        self.assertNotIn("img", out)
        self.assertIn("لینک", out)
        self.assertNotIn("\n\n\n", out)


def tavily_extract_response(url="https://example.com/a", body="بدنه‌ی مقاله. " * 3000):
    return FakeResponse({"results": [{"url": url, "raw_content": body}]})


class PrepareLinkPromptTests(unittest.TestCase):
    def test_bare_url_builds_default_summary_prompt(self):
        with mock.patch("requests.post", return_value=tavily_extract_response()) as post:
            prompt, memory, error = web.prepare_link_prompt("https://example.com/a")
        self.assertIsNone(error)
        self.assertIn("https://example.com/a", prompt)
        self.assertIn(web.LINK_DEFAULT_REQUEST, prompt)
        self.assertIn(web.LINK_PROMPT_RULES, prompt)  # prompt-injection guard is always present
        self.assertEqual(post.call_args.args[0], "https://api.tavily.com/extract")
        self.assertEqual(post.call_args.kwargs["json"]["urls"], ["https://example.com/a"])

    def test_page_text_is_capped_to_protect_the_token_budget(self):
        with mock.patch("requests.post", return_value=tavily_extract_response()):
            prompt, _, _ = web.prepare_link_prompt("https://example.com/a")
        self.assertLess(len(prompt), config.LINK_MAX_CHARS + 1500)

    def test_memory_note_is_short_not_the_page(self):
        with mock.patch("requests.post", return_value=tavily_extract_response()):
            _, memory, _ = web.prepare_link_prompt("https://example.com/a")
        self.assertTrue(memory.startswith("[لینک]"))
        self.assertLess(len(memory), 400)

    def test_user_instruction_replaces_default_request(self):
        with mock.patch("requests.post", return_value=tavily_extract_response()):
            prompt, _, _ = web.prepare_link_prompt("نکات مالی این را بگو https://example.com/a")
        self.assertIn("درخواست کاربر: نکات مالی این را بگو", prompt)
        self.assertNotIn(web.LINK_DEFAULT_REQUEST, prompt)

    def test_message_without_link_is_left_alone(self):
        self.assertEqual(web.prepare_link_prompt("سلام بدون لینک"), (None, None, None))

    def test_long_pasted_text_is_not_treated_as_a_link_request(self):
        text = "x" * (config.LINK_MAX_EXTRA_TEXT + 100) + " https://example.com"
        with mock.patch("requests.post") as post:
            self.assertEqual(web.prepare_link_prompt(text), (None, None, None))
        post.assert_not_called()

    def test_without_tavily_key_links_are_ignored(self):
        with mock.patch.object(config, "TAVILY_API_KEY", None):
            self.assertEqual(web.prepare_link_prompt("https://example.com"), (None, None, None))

    def test_only_the_first_links_are_fetched(self):
        text = "https://a.com/1 https://a.com/2 https://a.com/3"
        with mock.patch("requests.post", return_value=tavily_extract_response()) as post:
            web.prepare_link_prompt(text)
        self.assertEqual(len(post.call_args.kwargs["json"]["urls"]), config.LINK_MAX_COUNT)

    def test_http_failure_returns_friendly_error(self):
        with mock.patch("requests.post", return_value=FakeResponse({}, status=500)):
            self.assertEqual(web.prepare_link_prompt("https://example.com/a"), (None, None, web.LINK_ERROR_TEXT))

    def test_empty_page_returns_friendly_error(self):
        empty = FakeResponse({"results": [{"url": "u", "raw_content": "   "}]})
        with mock.patch("requests.post", return_value=empty):
            self.assertEqual(web.prepare_link_prompt("https://example.com/a"), (None, None, web.LINK_ERROR_TEXT))


class ToolReadTests(unittest.TestCase):
    def test_returns_full_text_with_larger_cap_than_summary(self):
        with mock.patch("requests.post", return_value=tavily_extract_response("https://blocked.example/news", "خبر. " * 5000)):
            out = web.tool_read("https://blocked.example/news")
        self.assertTrue(out.startswith("📄 https://blocked.example/news"))
        self.assertGreater(len(out), config.LINK_MAX_CHARS)
        self.assertLessEqual(len(out), config.READ_MAX_CHARS + 200)

    def test_usage_errors(self):
        with self.assertRaises(ToolError) as ctx:
            web.tool_read("no link")
        self.assertIn("/read", str(ctx.exception))

    def test_missing_key(self):
        with mock.patch.object(config, "TAVILY_API_KEY", None):
            with self.assertRaises(ToolError):
                web.tool_read("https://example.com")

    def test_failure_is_a_tool_error(self):
        with mock.patch("requests.post", return_value=FakeResponse({}, status=500)):
            with self.assertRaises(ToolError) as ctx:
                web.tool_read("https://example.com")
        self.assertEqual(str(ctx.exception), web.LINK_ERROR_TEXT)



class SkipRuleTests(unittest.TestCase):
    """Decisions that need no model: greetings and very short messages never search, explicit phrases always do."""

    def test_greetings_never_search_even_with_punctuation_or_emoji(self):
        for text in ("سلام", "سلام!", "  Hello  ", "hi 👋", "ممنون.", "چه خبر؟", "دمت گرم", "اوکی"):
            with self.subTest(text=text):
                self.assertIs(web.obvious_search_decision(text), False)

    def test_very_short_and_empty_messages_never_search(self):
        for text in ("", "   ", "ok", "؟", "👍", "!!!!", None):
            with self.subTest(text=text):
                self.assertIs(web.obvious_search_decision(text), False)

    def test_the_length_rule_is_the_old_one(self):
        self.assertIs(web.obvious_search_decision("طلا؟"), None)  # 4 characters: left to the planner
        self.assertIs(web.obvious_search_decision("نان"), False)  # 3 characters

    def test_explicit_phrases_always_search_and_beat_the_short_rule(self):
        for text in ("سرچ کن قیمت طلا", "لطفا جستجو کن", "گوگل کن", "search", "سرچ", "Search for python docs", "بگرد"):
            with self.subTest(text=text):
                self.assertIs(web.obvious_search_decision(text), True)

    def test_everything_else_is_left_to_the_planner(self):
        for text in ("قیمت دلار امروز چنده", "الگوریتم مرتب‌سازی حبابی را توضیح بده", "و یورو؟", "سلام قیمت دلار چنده"):
            with self.subTest(text=text):
                self.assertIsNone(web.obvious_search_decision(text))

    def test_the_keyword_heuristic_agrees_with_the_skip_rules(self):
        self.assertTrue(web.should_search_web("سرچ"))
        self.assertFalse(web.should_search_web("سلام!"))
        self.assertTrue(web.should_search_web("طلا؟"))  # unchanged: 4 characters and a freshness keyword

    def test_the_lists_live_in_web_service(self):
        self.assertIn("سرچ کن", web.EXPLICIT_SEARCH_PHRASES)
        self.assertIn("سلام", web.GREETINGS)
        self.assertEqual(web.MIN_SEARCHABLE_CHARS, 4)


class HeuristicQueryTests(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": "نتیجه بازی پرسپولیس؟"},
        {"role": "assistant", "content": "اطلاعاتی ندارم"},
    ]

    def test_the_query_is_the_cleaned_message(self):
        self.assertEqual(web.heuristic_query("سرچ کن قیمت طلا؟"), "قیمت طلا")
        self.assertEqual(web.heuristic_query("سرچ کن قیمت طلا؟", self.HISTORY), "قیمت طلا")

    def test_a_bare_search_request_uses_the_previous_user_message(self):
        for text in ("سرچ کن", "لطفا سرچ کن!", "گوگل کن", "search", "بگرد؟"):
            with self.subTest(text=text):
                self.assertEqual(web.heuristic_query(text, self.HISTORY), "نتیجه بازی پرسپولیس")

    def test_without_a_user_message_the_text_is_all_there_is(self):
        self.assertEqual(web.heuristic_query("سرچ کن"), "سرچ کن")
        self.assertEqual(web.heuristic_query("سرچ کن", []), "سرچ کن")
        self.assertEqual(web.heuristic_query("سرچ کن", [{"role": "assistant", "content": "x"}, "junk", None]), "سرچ کن")

    def test_the_most_recent_user_message_wins(self):
        history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "a"},
                   {"role": "user", "content": "قیمت سکه"}, {"role": "assistant", "content": "b"}]
        self.assertEqual(web.heuristic_query("سرچ کن", history), "قیمت سکه")

    def test_queries_are_one_bounded_line(self):
        query = web.heuristic_query("سرچ کن " + "کلمه\n" * 200)
        self.assertNotIn("\n", query)
        self.assertLessEqual(len(query), web.MAX_QUERY_CHARS)


class TTLCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = web.TTLCache(clock=self.clock)
        for name, value in (("SEARCH_CACHE_TTL_SECONDS", 600.0), ("SEARCH_CACHE_MAX_ENTRIES", 3)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_stored_value_is_returned_until_it_expires(self):
        self.cache.put("q", "result")
        self.assertEqual(self.cache.get("q"), "result")
        self.clock.advance(599.9)
        self.assertEqual(self.cache.get("q"), "result")
        self.clock.advance(0.1)          # exactly the TTL: expired
        self.assertIsNone(self.cache.get("q"))
        self.assertEqual(len(self.cache), 0)  # an expired entry is removed when it is found

    def test_unknown_keys_miss(self):
        self.assertIsNone(self.cache.get("nothing"))

    def test_the_ttl_is_read_at_call_time(self):
        self.cache.put("q", "result")
        with mock.patch.object(config, "SEARCH_CACHE_TTL_SECONDS", 10.0):
            self.cache.put("short", "x")
            self.clock.advance(11)
            self.assertIsNone(self.cache.get("short"))
        self.assertEqual(self.cache.get("q"), "result")  # stored with the 600 s TTL

    def test_a_ttl_of_zero_switches_the_cache_off(self):
        self.cache.put("q", "result")
        with mock.patch.object(config, "SEARCH_CACHE_TTL_SECONDS", 0):
            self.assertIsNone(self.cache.get("q"))
            self.cache.put("other", "x")
        self.assertEqual(len(self.cache), 1)  # nothing was stored while it was off

    def test_the_cache_is_bounded_and_drops_the_oldest_first(self):
        for key in ("a", "b", "c", "d"):
            self.cache.put(key, key.upper())
        self.assertEqual(len(self.cache), 3)
        self.assertIsNone(self.cache.get("a"))
        self.assertEqual([self.cache.get(k) for k in ("b", "c", "d")], ["B", "C", "D"])

    def test_storing_a_key_again_makes_it_the_newest(self):
        for key in ("a", "b", "c"):
            self.cache.put(key, key)
        self.cache.put("a", "A2")
        self.cache.put("d", "D")
        self.assertIsNone(self.cache.get("b"))
        self.assertEqual(self.cache.get("a"), "A2")

    def test_expired_entries_are_swept_when_something_is_stored(self):
        self.cache.put("a", 1)
        self.cache.put("b", 2)
        self.clock.advance(601)
        self.cache.put("c", 3)
        self.assertEqual(len(self.cache), 1)

    def test_clear(self):
        self.cache.put("a", 1)
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)

    def test_every_operation_takes_the_lock(self):
        class CountingLock:
            def __init__(self):
                self.acquired = 0

            def __enter__(self):
                self.acquired += 1

            def __exit__(self, *exc):
                return False

        lock = self.cache._lock = CountingLock()
        self.cache.put("a", 1)
        self.cache.get("a")
        len(self.cache)
        self.cache.clear()
        self.assertEqual(lock.acquired, 4)

    def test_concurrent_use_never_breaks_the_bound(self):
        errors = []
        with mock.patch.object(config, "SEARCH_CACHE_MAX_ENTRIES", 16):
            def worker(n):
                try:
                    for i in range(300):
                        self.cache.put(f"{n}-{i % 40}", i)
                        self.cache.get(f"{n}-{(i + 7) % 40}")
                        self.assertLessEqual(len(self.cache), 16)
                except Exception as e:  # noqa: BLE001 - collected and asserted below
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(self.cache), 16)


def tavily_search_response(results=None, answer="42"):
    results = results if results is not None else [
        {"title": "T1", "content": "C1", "url": "https://a.example/1"},
        {"title": "T2", "content": "C2", "url": "https://b.example/2"},
    ]
    return FakeResponse({"answer": answer, "results": results})


class CachedSearchTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = web.TTLCache(clock=self.clock)
        for patcher in (
            mock.patch.object(web, "search_cache", self.cache),
            mock.patch.object(config, "SEARCH_CACHE_TTL_SECONDS", 600.0),
            mock.patch.object(config, "SEARCH_CACHE_MAX_ENTRIES", 128),
            mock.patch.object(config, "SEARCH_MAX_SOURCES", 3),
            mock.patch("requests.get", side_effect=AssertionError("RSS must not be reached")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def search(self, query, **kwargs):
        sources = []
        text = web.fetch_web_context(query, sources=sources, cached=True, clean=False, **kwargs)
        return text, sources

    def test_an_identical_query_is_searched_once(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            first = self.search("قیمت دلار")
            second = self.search("قیمت دلار")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(first, second)
        self.assertIn("• T1: C1", first[0])
        self.assertEqual(first[1], ["https://a.example/1", "https://b.example/2"])  # sources come from the cache too

    def test_case_and_spacing_do_not_matter(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            self.search("Dollar   Price")
            self.search("  dollar price ")
        self.assertEqual(post.call_count, 1)

    def test_a_different_query_is_a_different_search(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            self.search("قیمت دلار")
            self.search("قیمت یورو")
        self.assertEqual(post.call_count, 2)

    def test_an_expired_entry_is_searched_again(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            self.search("قیمت دلار")
            self.clock.advance(599)
            self.search("قیمت دلار")
            self.assertEqual(post.call_count, 1)
            self.clock.advance(1)
            self.search("قیمت دلار")
        self.assertEqual(post.call_count, 2)

    def test_the_ttl_setting_controls_expiry_and_zero_disables_the_cache(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            with mock.patch.object(config, "SEARCH_CACHE_TTL_SECONDS", 0):
                self.search("قیمت دلار")
                self.search("قیمت دلار")
            self.assertEqual(post.call_count, 2)
            self.assertEqual(len(self.cache), 0)

    def test_a_failed_search_is_not_cached(self):
        with mock.patch("requests.post", side_effect=RuntimeError("down")), \
                mock.patch("requests.get", side_effect=RuntimeError("down")):
            self.assertEqual(self.search("قیمت دلار"), ("", []))
        self.assertEqual(len(self.cache), 0)
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            self.assertIn("T1", self.search("قیمت دلار")[0])
        self.assertEqual(post.call_count, 1)

    def test_web_and_testweb_stay_live_and_uncached(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            web.fetch_web_context("قیمت دلار")
            web.fetch_web_context("قیمت دلار")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(self.cache), 0)

    def test_web_never_reads_what_the_automatic_search_cached(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            self.search("قیمت دلار")                      # the automatic search caches this query
            web.fetch_web_context("قیمت دلار")            # /web asks the same thing and must still go live
        self.assertEqual(post.call_count, 2)

    def test_the_plain_call_returns_exactly_the_text_it_always_did(self):
        with mock.patch("requests.post", return_value=tavily_search_response()):
            self.assertEqual(web.fetch_web_context("قیمت دلار"), "• پاسخ سریع: 42\n• T1: C1\n• T2: C2")

    def test_planner_queries_are_not_cleaned_but_the_old_path_still_is(self):
        with mock.patch("requests.post", return_value=tavily_search_response()) as post:
            web.fetch_web_context("python 3.13 release", clean=False)
            self.assertEqual(post.call_args.kwargs["json"]["query"], "python 3.13 release")
            web.fetch_web_context("python 3.13 release")
            self.assertEqual(post.call_args.kwargs["json"]["query"], "python 313 release")  # the legacy cleaning, unchanged

    def test_a_cache_hit_still_honours_the_callers_sources_list(self):
        with mock.patch("requests.post", return_value=tavily_search_response()):
            self.search("قیمت دلار")
            sources = ["existing"]
            web.fetch_web_context("قیمت دلار", sources=sources, cached=True, clean=False)
        self.assertEqual(sources, ["existing", "https://a.example/1", "https://b.example/2"])


class SourceExtractionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "SEARCH_MAX_SOURCES", 3)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("requests.get", side_effect=AssertionError("RSS must not be reached"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sources_of(self, results):
        sources = []
        with mock.patch("requests.post", return_value=tavily_search_response(results)):
            web.fetch_web_context("قیمت دلار", sources=sources)
        return sources

    def test_at_most_three_links_without_duplicates_in_result_order(self):
        urls = ["https://a.example/1", "https://a.example/1", "https://b.example/2", "https://c.example/3", "https://d.example/4"]
        results = [{"title": "t", "content": "c", "url": u} for u in urls]
        self.assertEqual(self.sources_of(results), ["https://a.example/1", "https://b.example/2", "https://c.example/3"])

    def test_the_limit_is_a_setting(self):
        results = [{"title": "t", "content": "c", "url": f"https://x.example/{i}"} for i in range(5)]
        with mock.patch.object(config, "SEARCH_MAX_SOURCES", 1):
            self.assertEqual(self.sources_of(results), ["https://x.example/0"])

    def test_only_real_web_links_qualify(self):
        bad = ["javascript:alert(1)", "ftp://x.example/f", "/relative/path", "https://", "http://[bad", "", "   ", None, 5,
               "https://x.example/" + "a" * 600]
        results = [{"title": "t", "content": "c", "url": u} for u in bad]
        results.append({"title": "t", "content": "c"})  # no url key at all
        results.append({"title": "ok", "content": "c", "url": "  http://ok.example/page  "})
        self.assertEqual(self.sources_of(results), ["http://ok.example/page"])

    def test_an_answer_without_result_links_has_no_sources(self):
        self.assertEqual(self.sources_of([]), [])

    def test_the_rss_fallback_contributes_item_links(self):
        rss = (b"<rss><channel>"
               b"<item><title>H1</title><link>https://news.example/1</link></item>"
               b"<item><title>H2</title></item>"
               b"<item><title>H3</title><link>https://news.example/3</link></item>"
               b"</channel></rss>")
        sources = []
        with mock.patch.object(config, "TAVILY_API_KEY", None), \
                mock.patch("requests.get", return_value=FakeResponse(content=rss)):
            out = web.fetch_web_context("خبر", sources=sources)
        self.assertEqual(out, "• خبر: H1\n• خبر: H2\n• خبر: H3")
        self.assertEqual(sources, ["https://news.example/1", "https://news.example/3"])

    def test_no_sources_list_is_fine(self):
        with mock.patch("requests.post", return_value=tavily_search_response()):
            self.assertIn("T1", web.fetch_web_context("قیمت دلار"))


class FormatSourcesTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "SEARCH_MAX_SOURCES", 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_label_and_numbered_links(self):
        out = web.format_sources(["https://a.example/1", "https://b.example/2"])
        self.assertEqual(out, "\n\nمنابع:\n1. https://a.example/1\n2. https://b.example/2")
        self.assertEqual(web.SOURCES_LABEL, "منابع:")

    def test_nothing_usable_gives_an_empty_string(self):
        for urls in (None, [], (), ["ftp://x"], [None, 3, ""], ["not a url"]):
            with self.subTest(urls=urls):
                self.assertEqual(web.format_sources(urls), "")

    def test_never_more_than_three_links(self):
        out = web.format_sources([f"https://x.example/{i}" for i in range(6)])
        self.assertEqual(out.count("https://"), 3)
        self.assertNotIn("4.", out)

    def test_duplicates_are_listed_once(self):
        self.assertEqual(web.format_sources(["https://a.example/1"] * 3).count("https://"), 1)

    def test_characters_that_break_chat_rendering_are_percent_encoded(self):
        out = web.format_sources(["https://x.example/a b/**bold**/`code`/<i>/\"q\""])
        line = out.splitlines()[-1]
        for char in (" ", "*", "`", "<", ">", '"'):
            self.assertNotIn(char, line.split(" ", 1)[1])
        self.assertIn("%20", line)
        self.assertIn("%2A%2Abold%2A%2A", line)
        self.assertIn("%60code%60", line)

    def test_a_valid_link_is_kept_as_it_is(self):
        url = "https://en.wikipedia.org/wiki/Foo_(bar)?a=1&b=2%20c#top"
        self.assertEqual(web.format_sources([url]), f"\n\nمنابع:\n1. {url}")

    def test_non_ascii_links_become_plain_ascii(self):
        line = web.format_sources(["https://fa.wikipedia.org/wiki/ایران"]).splitlines()[-1]
        self.assertTrue(line.isascii())
        self.assertIn("%D8%A7", line)

    def test_the_block_survives_the_telegram_html_conversion(self):
        block = web.format_sources(["https://x.example/p?a=1&b=2", "https://y.example/**x**/`c`"])
        html_text = to_telegram_html("پاسخ" + block, escape=True)
        self.assertIn("https://x.example/p?a=1&amp;b=2", html_text)  # escaped once; Telegram shows a plain &
        self.assertNotIn("<b>", html_text)
        self.assertNotIn("<code>", html_text)
        self.assertNotIn("&amp;amp;", html_text)


if __name__ == "__main__":
    unittest.main()
