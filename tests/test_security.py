import io
import logging
import unittest
from unittest import mock

import config
from services import ai_router as router
from services import clients
from services.key_pool import KeyPool
from tests.helpers import FakeClock, FakeHttpError, pin_rotation_settings
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


class RotatedKeyRedactionTests(unittest.TestCase):
    """Every key of a ``*_API_KEYS`` list is a secret, not only the singular variables."""
    LISTS = {
        "GROQ_API_KEYS": ["groq-rot-AAAA1111", "groq-rot-BBBB2222"],
        "GEMINI_API_KEYS": ["gemini-rot-AAAA1111", "gemini-rot-BBBB2222"],
        "TAVILY_API_KEYS": ["tavily-rot-AAAA1111", "tavily-rot-BBBB2222"],
    }

    def setUp(self):
        for name, keys in self.LISTS.items():
            patcher = mock.patch.object(config, name, keys)
            patcher.start()
            self.addCleanup(patcher.stop)

    def all_keys(self):
        return [key for keys in self.LISTS.values() for key in keys]

    def test_every_key_of_every_list_is_masked(self):
        out = redact_secrets("; ".join(self.all_keys()))
        for key in self.all_keys():
            self.assertNotIn(key, out)
        self.assertEqual(out.count("***"), 6)

    def test_singular_variables_are_still_masked_next_to_the_lists(self):
        out = redact_secrets(f"{config.GROQ_API_KEY} {config.GEMINI_API_KEY} {config.TAVILY_API_KEY} {self.all_keys()[0]}")
        self.assertEqual(out, "*** *** *** ***")

    def test_a_key_that_is_a_prefix_of_another_leaves_no_readable_tail(self):
        with mock.patch.object(config, "GROQ_API_KEYS", ["prefix-key", "prefix-key-with-a-long-tail"]):
            self.assertEqual(redact_secrets("a prefix-key-with-a-long-tail b prefix-key c"), "a *** b *** c")

    def test_short_err_masks_a_rotated_key_inside_an_exception(self):
        out = short_err(RuntimeError(f"401 Incorrect API key provided: {self.LISTS['GEMINI_API_KEYS'][1]}"))
        self.assertNotIn("gemini-rot", out)
        self.assertIn("***", out)

    def test_the_combined_failover_error_does_not_leak_any_key(self):
        def groq_down(*args, **kwargs):
            raise RuntimeError(f"401 for {self.LISTS['GROQ_API_KEYS'][0]}")

        def gemini_down(*args, **kwargs):
            raise RuntimeError(f"403 for {self.LISTS['GEMINI_API_KEYS'][1]}")

        with mock.patch.object(clients, "groq_client", object()), mock.patch.object(clients, "gemini_client", object()), \
                mock.patch.object(router, "call_groq", groq_down), mock.patch.object(router, "call_gemini", gemini_down):
            with self.assertRaises(RuntimeError) as ctx:
                router.process_ai_query("q", model_name=config.MODEL_FAST, use_web=False)
        for key in self.all_keys():
            self.assertNotIn(key, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_rotation_logs_and_pool_errors_never_contain_a_key(self):
        pin_rotation_settings(self)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s [%(name)s] %(message)s"))
        logger = logging.getLogger("services.key_pool")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(previous_level)))

        keys = self.LISTS["GROQ_API_KEYS"]
        pool = KeyPool("groq", lambda: keys, clock=FakeClock())

        def fn(key):
            raise FakeHttpError(429, message=f"rate limited for {key}")

        with self.assertRaises(FakeHttpError):
            pool.call(fn)
        with self.assertRaises(RuntimeError) as cooling:
            pool.call(fn)
        output = stream.getvalue() + str(cooling.exception)
        self.assertIn("groq key #1/2", output)  # provider and position are logged ...
        for key in self.all_keys():
            self.assertNotIn(key, output)       # ... but never any part of a key
            self.assertNotIn(key[:8], output)


if __name__ == "__main__":
    unittest.main()
