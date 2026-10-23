import io
import logging
import unittest
from unittest import mock

import config
from utils.log import RedactingFormatter
from utils.security import is_authorized, is_authorized_bale, redact_secrets, short_err


class AuthorizationTests(unittest.TestCase):
    def test_empty_allow_list_means_open(self):
        with mock.patch.object(config, "ALLOWED_USERS", []):
            self.assertTrue(is_authorized(1))
            self.assertTrue(is_authorized_bale(1, 2))

    def test_allow_list_blocks_strangers(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            self.assertTrue(is_authorized(111))
            self.assertFalse(is_authorized(222))

    def test_bale_accepts_allow_listed_sender_or_chat(self):
        with mock.patch.object(config, "ALLOWED_USERS", [111]):
            self.assertTrue(is_authorized_bale(111, 999))
            self.assertTrue(is_authorized_bale(999, 111))
            self.assertFalse(is_authorized_bale(999, 998))

    def test_allowed_users_parsing_ignores_junk(self):
        self.assertEqual(config._parse_user_ids("1, 2,abc,,3 "), [1, 2, 3])
        self.assertEqual(config._parse_user_ids(""), [])
        self.assertEqual(config._parse_user_ids(None), [])


class RedactionTests(unittest.TestCase):
    def test_all_configured_secrets_are_masked(self):
        text = (
            f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/x "
            f"{config.BALE_BOT_TOKEN} {config.GROQ_API_KEY} {config.GEMINI_API_KEY} {config.TAVILY_API_KEY}"
        )
        out = redact_secrets(text)
        for secret in (config.TELEGRAM_BOT_TOKEN, config.BALE_BOT_TOKEN, config.GROQ_API_KEY,
                       config.GEMINI_API_KEY, config.TAVILY_API_KEY):
            self.assertNotIn(secret, out)
        self.assertIn("***", out)

    def test_missing_secrets_do_not_break_redaction(self):
        with mock.patch.object(config, "GROQ_API_KEY", None):
            self.assertEqual(redact_secrets("plain"), "plain")

    def test_short_err_is_redacted_and_truncated(self):
        err = RuntimeError(f"boom {config.GROQ_API_KEY} " + "x" * 500)
        out = short_err(err)
        self.assertTrue(out.startswith("RuntimeError: boom ***"))
        self.assertNotIn(config.GROQ_API_KEY, out)
        self.assertLess(len(out), 260)

    def test_log_formatter_redacts_messages_and_tracebacks(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("redaction-test")
        logger.handlers[:] = [handler]
        logger.propagate = False
        try:
            raise RuntimeError(f"failed with {config.TELEGRAM_BOT_TOKEN}")
        except RuntimeError:
            logger.error("token=%s", config.TELEGRAM_BOT_TOKEN, exc_info=True)
        output = stream.getvalue()
        self.assertNotIn(config.TELEGRAM_BOT_TOKEN, output)
        self.assertIn("***", output)


if __name__ == "__main__":
    unittest.main()
