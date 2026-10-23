import unittest
from unittest import mock

import config
from core.errors import ToolError
from services import web_service as web
from tests.helpers import FakeResponse


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


if __name__ == "__main__":
    unittest.main()
