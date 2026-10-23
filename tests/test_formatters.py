import unittest

from utils.formatters import split_message, to_telegram_html, truncate_text


class SplitMessageTests(unittest.TestCase):
    def test_short_text_is_a_single_chunk(self):
        self.assertEqual(split_message("hello", 100), ["hello"])

    def test_chunks_respect_the_limit_and_break_on_lines(self):
        text = "\n".join(f"line {i}" for i in range(400))
        chunks = split_message(text, 500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        # nothing is lost or duplicated
        self.assertEqual("\n".join(chunks).split("\n"), text.split("\n"))

    def test_code_fences_stay_balanced_in_every_chunk(self):
        code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(1500)) + "\n```"
        chunks = split_message(code, 3500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0, "a chunk has an unbalanced code fence")
        # later chunks re-open the block with a fence on their first line
        self.assertTrue(chunks[1].startswith("```"))

    def test_text_outside_code_is_not_fenced(self):
        text = "intro\n```\ncode\n```\n" + "\n".join("plain line" for _ in range(300))
        chunks = split_message(text, 400)
        self.assertFalse(chunks[-1].startswith("```"))

    def test_single_very_long_line_is_hard_wrapped(self):
        chunks = split_message("a" * 5000, 1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), "a" * 5000)

    def test_empty_input(self):
        self.assertEqual(split_message("", 100), [""])
        self.assertEqual(split_message(None, 100), [""])


class ToTelegramHtmlTests(unittest.TestCase):
    def test_escape_neutralises_html(self):
        self.assertEqual(to_telegram_html("a < b & c > d", escape=True), "a &lt; b &amp; c &gt; d")

    def test_without_escape_existing_tags_pass_through(self):
        self.assertEqual(to_telegram_html("<b>x</b>"), "<b>x</b>")

    def test_bold_and_inline_code(self):
        self.assertEqual(to_telegram_html("**bold** and `code`"), "<b>bold</b> and <code>code</code>")

    def test_double_star_inside_code_is_not_bold(self):
        self.assertEqual(to_telegram_html("`**kwargs**`"), "<code>**kwargs**</code>")

    def test_fenced_block_becomes_pre_and_language_tag_is_dropped(self):
        out = to_telegram_html("```python\nprint('x')\n```")
        self.assertEqual(out, "<pre>print('x')\n</pre>")

    def test_code_with_angle_brackets_survives_escaping(self):
        out = to_telegram_html("```\nif a < b:\n```", escape=True)
        self.assertIn("a &lt; b", out)
        self.assertTrue(out.startswith("<pre>"))


class TruncateTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(truncate_text("abc", 10), "abc")

    def test_cut_prefers_a_line_boundary_and_adds_marker(self):
        text = ("word " * 20 + "\n") * 30
        out = truncate_text(text, 500)
        self.assertLessEqual(len(out), 500 + 60)
        self.assertIn("ادامه‌ی متن برای کوتاه‌سازی حذف شد", out)
        self.assertTrue(out.startswith("word word"))


if __name__ == "__main__":
    unittest.main()
