import unittest
from unittest import mock

import config
from core.errors import ToolError
from services import clients
from services import knowledge_service as ks
from tests.helpers import FakeResponse


def wiki_get(url, params=None, headers=None, timeout=None):
    lang = url.split("//")[1].split(".")[0]
    if params.get("list") == "search":
        if lang == "fa" and "ناموجود" in params["srsearch"]:
            return FakeResponse({"query": {"search": []}})
        title = "Dijkstra's algorithm" if lang == "en" else "الگوریتم دایکسترا"
        return FakeResponse({"query": {"search": [{"title": title}]}})
    extract = "Intro text.\n\n\n== History ==\nSome history.\n=== Detail ===\nMore.\n== See also ==\nLinks\n== References ==\nRefs"
    if lang == "fa":
        extract = "مقدمه.\n== تاریخچه ==\nمتن.\n== منابع ==\nمنبع‌ها"
    return FakeResponse({"query": {"pages": [{"title": "x", "extract": extract}]}})


class WikipediaTests(unittest.TestCase):
    def test_english_topic_uses_english_wikipedia_first_and_cuts_tail_sections(self):
        with mock.patch("requests.get", side_effect=wiki_get) as get:
            out = ks.tool_wiki("Dijkstra")
        self.assertIn("en.wikipedia.org", get.call_args_list[0].args[0])
        self.assertIn("▪️ History", out)
        self.assertIn("▪️ Detail", out)
        self.assertNotIn("References", out)
        self.assertNotIn("See also", out)
        self.assertTrue(out.startswith("📚 ویکی‌پدیا (en) — Dijkstra's algorithm"))

    def test_persian_topic_uses_persian_wikipedia(self):
        with mock.patch("requests.get", side_effect=wiki_get) as get:
            out = ks.tool_wiki("دایکسترا")
        self.assertIn("fa.wikipedia.org", get.call_args_list[0].args[0])
        self.assertIn("▪️ تاریخچه", out)
        self.assertNotIn("منابع", out)

    def test_persian_miss_falls_back_to_english(self):
        with mock.patch("requests.get", side_effect=wiki_get) as get:
            out = ks.tool_wiki("ناموجود")
        languages = [c.args[0].split("//")[1].split(".")[0] for c in get.call_args_list if c.kwargs["params"].get("list") == "search"]
        self.assertEqual(languages, ["fa", "en"])
        self.assertIn("(en)", out)

    def test_output_is_capped(self):
        def long_get(url, params=None, headers=None, timeout=None):
            if params.get("list") == "search":
                return FakeResponse({"query": {"search": [{"title": "T"}]}})
            return FakeResponse({"query": {"pages": [{"extract": "جمله‌ی طولانی. " * 5000}]}})
        with mock.patch("requests.get", side_effect=long_get):
            out = ks.tool_wiki("topic")
        self.assertLessEqual(len(out), config.WIKI_MAX_CHARS + 200)

    def test_network_failure_is_friendly(self):
        with mock.patch("requests.get", side_effect=RuntimeError("down")):
            with self.assertRaises(ToolError) as ctx:
                ks.tool_wiki("x")
        self.assertIn("ارتباط با ویکی‌پدیا", str(ctx.exception))

    def test_usage_error(self):
        with self.assertRaises(ToolError) as ctx:
            ks.tool_wiki("")
        self.assertIn("/wiki", str(ctx.exception))

    def test_user_agent_header_is_sent(self):
        with mock.patch("requests.get", side_effect=wiki_get) as get:
            ks.tool_wiki("Dijkstra")
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])

    def test_heading_formatting_unit(self):
        text = "a\n== Intro ==\nb\n== Notes ==\nc"
        self.assertEqual(ks.format_wiki_text(text), "a\n▪️ Intro\nb")


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>http://arxiv.org/abs/1706.03762v7</id><published>2017-06-12T17:57:34Z</published>
  <title>Attention Is All
   You Need</title><summary>  The dominant sequence
   transduction models...  </summary>
  <author><name>Ashish Vaswani</name></author><author><name>Noam Shazeer</name></author>
  <author><name>A</name></author><author><name>B</name></author><author><name>C</name></author>
  <category term="cs.CL"/></entry>
 <entry><id>http://arxiv.org/abs/1810.04805v2</id><published>2018-10-11T00:00:00Z</published>
  <title>BERT</title><summary>x</summary><author><name>Jacob Devlin</name></author><category term="cs.CL"/></entry>
</feed>"""
ERROR_FEED = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors</id><title>Error</title><summary>bad</summary></entry></feed>"""


class ArxivIdTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(ks.parse_arxiv_id("1706.03762"), "1706.03762")
        self.assertEqual(ks.parse_arxiv_id("arXiv:1706.03762v2"), "1706.03762v2")
        self.assertEqual(ks.parse_arxiv_id("https://arxiv.org/abs/1706.03762"), "1706.03762")
        self.assertEqual(ks.parse_arxiv_id("https://arxiv.org/pdf/1706.03762.pdf"), "1706.03762")
        self.assertEqual(ks.parse_arxiv_id("hep-th/9901001"), "hep-th/9901001")

    def test_free_text_is_never_an_id(self):
        self.assertIsNone(ks.parse_arxiv_id("attention is all you need"))
        self.assertIsNone(ks.parse_arxiv_id("gpt 2023.1234 paper"))
        self.assertIsNone(ks.parse_arxiv_id(""))


class ArxivToolTests(unittest.TestCase):
    def test_search_builds_an_and_query_and_lists_results(self):
        with mock.patch("requests.get", return_value=FakeResponse(content=ATOM.encode())) as get:
            out = ks.tool_arxiv("attention is all you need")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["search_query"], "all:attention AND all:is AND all:all AND all:you AND all:need")
        self.assertEqual(params["max_results"], config.ARXIV_RESULTS)
        self.assertIn("1) Attention Is All You Need", out)  # whitespace from the XML is normalised
        self.assertIn("🆔 1706.03762v7", out)
        self.assertIn("2) BERT", out)
        self.assertIn("و همکاران", out)  # more than four authors are abbreviated

    def test_id_lookup_returns_abstract_and_full_text_hint(self):
        with mock.patch("requests.get", return_value=FakeResponse(content=ATOM.encode())) as get:
            out = ks.tool_arxiv("1706.03762")
        self.assertEqual(get.call_args.kwargs["params"], {"id_list": "1706.03762"})
        self.assertIn("چکیده:\nThe dominant sequence transduction models...", out)
        self.assertIn("/arxiv_full 1706.03762v7", out)

    def test_api_error_entry_is_ignored(self):
        with mock.patch("requests.get", return_value=FakeResponse(content=ERROR_FEED.encode())):
            with self.assertRaises(ToolError) as ctx:
                ks.tool_arxiv("zzzz")
        self.assertIn("پیدا نشد", str(ctx.exception))

    def test_network_failure_is_friendly(self):
        with mock.patch("requests.get", side_effect=RuntimeError("timeout")):
            with self.assertRaises(ToolError) as ctx:
                ks.tool_arxiv("bert")
        self.assertIn("ارتباط با ArXiv", str(ctx.exception))

    def test_usage_error(self):
        with self.assertRaises(ToolError):
            ks.tool_arxiv("")


class ArxivFullTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(clients, "gemini_client", object())
        p.start()
        self.addCleanup(p.stop)

    def test_pdf_is_downloaded_and_sent_to_gemini(self):
        pdf = FakeResponse(content=b"%PDF-1.5 " + b"x" * 1000)
        with mock.patch("requests.get", return_value=pdf) as get, \
                mock.patch.object(ks, "process_ai_query", return_value=("SUMMARY", "")) as process:
            out = ks.tool_arxiv_full("1706.03762")
        self.assertEqual(get.call_args.args[0], "https://arxiv.org/pdf/1706.03762")
        image = process.call_args.kwargs["image"]
        self.assertEqual(image["mime"], "application/pdf")
        self.assertTrue(image["data"].startswith(b"%PDF"))
        self.assertEqual(out, "📄 ArXiv:1706.03762\n\nSUMMARY")

    def test_non_pdf_payload_is_rejected(self):
        with mock.patch("requests.get", return_value=FakeResponse(content=b"<html>nope</html>")):
            with self.assertRaises(ToolError) as ctx:
                ks.tool_arxiv_full("1706.03762")
        self.assertIn("معتبر نبود", str(ctx.exception))

    def test_oversize_pdf_is_rejected(self):
        big = FakeResponse(content=b"%PDF" + b"x" * (config.MAX_PDF_BYTES + 1))
        with mock.patch("requests.get", return_value=big):
            with self.assertRaises(ToolError):
                ks.tool_arxiv_full("1706.03762")

    def test_requires_an_id_and_a_gemini_key(self):
        with self.assertRaises(ToolError):
            ks.tool_arxiv_full("not an id")
        with mock.patch.object(clients, "gemini_client", None):
            with self.assertRaises(ToolError) as ctx:
                ks.tool_arxiv_full("1706.03762")
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
