import importlib
import os
import threading
import unittest
from collections import Counter
from unittest import mock

import config
from services import key_pool
from services.key_pool import (
    AUTH_FAILED, EMPTY_ANSWER, RATE_LIMITED, TRANSIENT, AllKeysCoolingDown, Failure, KeyPool,
    classify_exception, classify_status, cooldown_seconds, retry_after_seconds,
)
from tests.helpers import (
    APIConnectionError, APITimeoutError, FakeClock, FakeGenaiError, FakeHttpError, pin_rotation_settings,
)


class ReloadedConfig:
    """Re-import ``config`` under a patched environment and restore it afterwards."""

    def __init__(self, **env):
        self._patch = mock.patch.dict(os.environ, env)

    def __enter__(self):
        self._patch.start()
        return importlib.reload(config)

    def __exit__(self, *exc_info):
        self._patch.stop()
        importlib.reload(config)


class KeyParsingTests(unittest.TestCase):
    def test_spaces_empty_items_and_duplicates_are_tolerated(self):
        self.assertEqual(config.parse_api_keys(" k1 , k2,, k1 ,k3, "), ["k1", "k2", "k3"])
        self.assertEqual(config.parse_api_keys(",, ,"), [])

    def test_nothing_configured_gives_an_empty_list(self):
        self.assertEqual(config.parse_api_keys(None), [])
        self.assertEqual(config.parse_api_keys(""), [])
        self.assertEqual(config.parse_api_keys(None, [], None), [])

    def test_lists_are_cleaned_like_strings(self):
        self.assertEqual(config.parse_api_keys(["a", " b ", "", None, "a"]), ["a", "b"])

    def test_singular_comes_first_then_the_list_without_duplicates(self):
        with mock.patch.object(config, "GROQ_API_KEY", "k0"), \
                mock.patch.object(config, "GROQ_API_KEYS", ["k1", "k0", "k2"]):
            self.assertEqual(config.api_keys("groq"), ["k0", "k1", "k2"])

    def test_singular_alone_still_works(self):
        with mock.patch.object(config, "GEMINI_API_KEY", "only"), mock.patch.object(config, "GEMINI_API_KEYS", []):
            self.assertEqual(config.api_keys("gemini"), ["only"])

    def test_list_alone_works_without_the_singular_variable(self):
        with mock.patch.object(config, "TAVILY_API_KEY", None), mock.patch.object(config, "TAVILY_API_KEYS", ["t1", "t2"]):
            self.assertEqual(config.api_keys("tavily"), ["t1", "t2"])

    def test_nothing_configured(self):
        with mock.patch.object(config, "GROQ_API_KEY", None), mock.patch.object(config, "GROQ_API_KEYS", []):
            self.assertEqual(config.api_keys("groq"), [])

    def test_environment_variables_are_parsed_and_merged(self):
        env = {"GROQ_API_KEY": "g0", "GROQ_API_KEYS": " g1 , g2,,g1 ", "GEMINI_API_KEYS": "m1,m2",
               "TAVILY_API_KEYS": "  "}
        with ReloadedConfig(**env) as cfg:
            self.assertEqual(cfg.GROQ_API_KEYS, ["g1", "g2"])
            self.assertEqual(cfg.api_keys("groq"), ["g0", "g1", "g2"])
            self.assertEqual(cfg.api_keys("gemini"), [cfg.GEMINI_API_KEY, "m1", "m2"])
            self.assertEqual(cfg.TAVILY_API_KEYS, [])

    def test_rotation_constants_are_overridable_by_environment(self):
        env = {"KEY_MAX_ATTEMPTS": "5", "KEY_RETRY_BUDGET_SECONDS": "30", "KEY_COOLDOWN_RATE_LIMIT": "2.5",
               "KEY_COOLDOWN_RATE_LIMIT_MAX": "10", "KEY_COOLDOWN_TRANSIENT": "1", "KEY_COOLDOWN_AUTH": "120"}
        with ReloadedConfig(**env) as cfg:
            self.assertEqual(cfg.KEY_MAX_ATTEMPTS, 5)
            self.assertEqual(cfg.KEY_RETRY_BUDGET_SECONDS, 30.0)
            self.assertEqual(cfg.KEY_COOLDOWN_RATE_LIMIT, 2.5)
            self.assertEqual(cfg.KEY_COOLDOWN_RATE_LIMIT_MAX, 10.0)
            self.assertEqual(cfg.KEY_COOLDOWN_TRANSIENT, 1.0)
            self.assertEqual(cfg.KEY_COOLDOWN_AUTH, 120.0)

    def test_malformed_overrides_fall_back_to_the_defaults(self):
        defaults = {name: getattr(config, name) for name in (
            "KEY_MAX_ATTEMPTS", "KEY_RETRY_BUDGET_SECONDS", "KEY_COOLDOWN_RATE_LIMIT",
            "KEY_COOLDOWN_RATE_LIMIT_MAX", "KEY_COOLDOWN_TRANSIENT", "KEY_COOLDOWN_AUTH")}
        junk = {"KEY_MAX_ATTEMPTS": "0", "KEY_RETRY_BUDGET_SECONDS": "0", "KEY_COOLDOWN_RATE_LIMIT": "abc",
                "KEY_COOLDOWN_RATE_LIMIT_MAX": "-5", "KEY_COOLDOWN_TRANSIENT": "nan", "KEY_COOLDOWN_AUTH": "inf"}
        with ReloadedConfig(**junk) as cfg:
            for name, value in defaults.items():
                self.assertEqual(getattr(cfg, name), value, name)

    def test_defaults_are_sane(self):
        self.assertGreaterEqual(config.KEY_MAX_ATTEMPTS, 2)
        self.assertLess(config.KEY_COOLDOWN_TRANSIENT, config.KEY_COOLDOWN_RATE_LIMIT)
        self.assertLessEqual(config.KEY_COOLDOWN_RATE_LIMIT, config.KEY_COOLDOWN_RATE_LIMIT_MAX)
        self.assertGreater(config.KEY_COOLDOWN_AUTH, config.KEY_COOLDOWN_RATE_LIMIT_MAX)


class ClassificationTests(unittest.TestCase):
    def test_status_mapping(self):
        for status, kind in ((401, AUTH_FAILED), (403, AUTH_FAILED), (429, RATE_LIMITED),
                             (408, TRANSIENT), (500, TRANSIENT), (502, TRANSIENT), (503, TRANSIENT), (529, TRANSIENT)):
            with self.subTest(status=status):
                self.assertEqual(classify_status(status).kind, kind)

    def test_request_errors_and_success_do_not_rotate(self):
        for status in (200, 301, 400, 404, 409, 413, 422, None, "429", True):
            with self.subTest(status=status):
                self.assertIsNone(classify_status(status))

    def test_exception_shapes_of_the_sdks(self):
        cases = {
            "groq/requests style (.status_code)": (FakeHttpError(429), RATE_LIMITED),
            "genai style (.code)": (FakeGenaiError(429, "RESOURCE_EXHAUSTED", "quota"), RATE_LIMITED),
            "genai 503": (FakeGenaiError(503, "UNAVAILABLE", "overloaded"), TRANSIENT),
            "response.status_code only": (type("E", (Exception,), {"response": mock.Mock(status_code=502)})(), TRANSIENT),
            "401": (FakeHttpError(401), AUTH_FAILED),
            "403": (FakeGenaiError(403, "PERMISSION_DENIED"), AUTH_FAILED),
        }
        for name, (exc, kind) in cases.items():
            with self.subTest(name):
                self.assertEqual(classify_exception(exc).kind, kind)

    def test_network_failures_are_recognised_by_type_or_name(self):
        for exc in (TimeoutError(), ConnectionResetError(), APITimeoutError("t"), APIConnectionError("c"),
                    type("ReadTimeout", (Exception,), {})(), type("ConnectError", (Exception,), {})(),
                    type("ReadError", (type("NetworkError", (Exception,), {}),), {})()):
            with self.subTest(type(exc).__name__):
                self.assertEqual(classify_exception(exc), Failure(TRANSIENT))

    def test_request_errors_and_bugs_are_not_rotatable(self):
        for exc in (FakeHttpError(400), FakeHttpError(404), FakeHttpError(413), FakeGenaiError(400, "INVALID_ARGUMENT", "bad"),
                    ValueError("bug"), RuntimeError("boom"), KeyError("k")):
            with self.subTest(repr(exc)):
                self.assertIsNone(classify_exception(exc))

    def test_gemini_reports_an_invalid_key_as_400_and_that_is_an_auth_failure(self):
        exc = FakeGenaiError(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")
        self.assertEqual(classify_exception(exc).kind, AUTH_FAILED)
        self.assertEqual(classify_exception(Exception("x")), None)

    def test_retry_after_is_read_from_the_response_headers(self):
        self.assertEqual(classify_exception(FakeHttpError(429, {"Retry-After": "12"})), Failure(RATE_LIMITED, 12.0))
        self.assertEqual(classify_exception(FakeHttpError(429, {"retry-after": "0.5"})), Failure(RATE_LIMITED, 0.5))
        self.assertEqual(classify_exception(FakeHttpError(429)), Failure(RATE_LIMITED, None))

    def test_retry_after_parsing(self):
        self.assertEqual(retry_after_seconds({"RETRY-AFTER": "7"}), 7.0)
        for bad in (None, {}, {"retry-after": "soon"}, {"retry-after": "-3"}, {"retry-after": "inf"},
                    {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"x": "1"}, 5):
            with self.subTest(bad=bad):
                self.assertIsNone(retry_after_seconds(bad))

    def test_cooldown_per_failure_kind(self):
        with mock.patch.object(config, "KEY_COOLDOWN_RATE_LIMIT", 60.0), \
                mock.patch.object(config, "KEY_COOLDOWN_RATE_LIMIT_MAX", 300.0), \
                mock.patch.object(config, "KEY_COOLDOWN_TRANSIENT", 15.0), \
                mock.patch.object(config, "KEY_COOLDOWN_AUTH", 3600.0):
            self.assertEqual(cooldown_seconds(Failure(RATE_LIMITED)), 60.0)
            self.assertEqual(cooldown_seconds(Failure(RATE_LIMITED, 10.0)), 10.0)
            self.assertEqual(cooldown_seconds(Failure(RATE_LIMITED, 99999.0)), 300.0)  # capped
            self.assertEqual(cooldown_seconds(Failure(RATE_LIMITED, 0.0)), 0.0)
            self.assertEqual(cooldown_seconds(Failure(TRANSIENT)), 15.0)
            self.assertEqual(cooldown_seconds(Failure(AUTH_FAILED)), 3600.0)
            self.assertEqual(cooldown_seconds(EMPTY_ANSWER), 0.0)  # about the prompt, not the key


class PoolTestCase(unittest.TestCase):
    """A pool over fake keys with a fake clock; ``fn`` scripts one outcome per call and key."""

    def setUp(self):
        self.clock = FakeClock()
        self.calls = []
        pin_rotation_settings(self)

    def pool(self, *keys):
        return KeyPool("test", lambda: list(keys), clock=self.clock)

    def fn(self, script=None, default="ok"):
        script = {key: list(outcomes) for key, outcomes in (script or {}).items()}

        def run(key):
            self.calls.append(key)
            outcomes = script.get(key)
            outcome = outcomes.pop(0) if outcomes else default
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return run


class RoundRobinTests(PoolTestCase):
    def test_keys_are_used_in_turn(self):
        pool, fn = self.pool("a", "b", "c"), self.fn()
        for _ in range(7):
            pool.call(fn)
        self.assertEqual(self.calls, list("abcabca"))

    def test_a_cooling_key_is_skipped(self):
        pool = self.pool("a", "b", "c")
        pool.call(self.fn({"a": [FakeHttpError(429)]}))
        self.calls.clear()
        fn = self.fn()
        for _ in range(4):
            pool.call(fn)
        self.assertEqual(self.calls, ["c", "b", "c", "b"])  # a is never offered

    def test_cooldown_expires_exactly_when_it_ends(self):
        pool = self.pool("a", "b")
        pool.call(self.fn({"a": [FakeHttpError(429)]}))  # a: 60 s
        self.calls.clear()
        fn = self.fn()
        pool.call(fn)
        self.clock.advance(59.9)
        pool.call(fn)
        self.assertEqual(self.calls, ["b", "b"])
        self.clock.advance(0.1)
        pool.call(fn)
        self.assertEqual(self.calls, ["b", "b", "a"])

    def test_retry_after_is_honoured(self):
        pool = self.pool("a", "b")
        pool.call(self.fn({"a": [FakeHttpError(429, {"Retry-After": "10"})]}))
        self.calls.clear()
        fn = self.fn()
        self.clock.advance(9.9)
        pool.call(fn)
        self.clock.advance(0.1)
        pool.call(fn)
        self.assertEqual(self.calls, ["b", "a"])

    def test_retry_after_is_capped(self):
        pool = self.pool("a", "b")
        pool.call(self.fn({"a": [FakeHttpError(429, {"Retry-After": "86400"})]}))
        self.calls.clear()
        fn = self.fn()
        self.clock.advance(299.9)
        pool.call(fn)
        self.clock.advance(0.1)
        pool.call(fn)
        self.assertEqual(self.calls, ["b", "a"])

    def test_the_key_list_is_read_at_call_time(self):
        keys = ["a", "b"]
        pool = KeyPool("test", lambda: keys, clock=self.clock)
        fn = self.fn()
        pool.call(fn)
        pool.call(fn)
        keys.append("c")  # a key added later joins the rotation
        for _ in range(3):
            pool.call(fn)
        self.assertEqual(self.calls, ["a", "b", "a", "b", "c"])

    def test_a_shrinking_key_list_never_breaks_the_cursor(self):
        keys = ["a", "b", "c"]
        pool = KeyPool("test", lambda: keys, clock=self.clock)
        fn = self.fn()
        pool.call(fn)
        pool.call(fn)           # the cursor now points at index 2 ...
        keys[:] = ["a", "b"]    # ... which no longer exists
        pool.call(fn)
        pool.call(fn)
        self.assertEqual(self.calls, ["a", "b", "a", "b"])


class RotationTriggerTests(PoolTestCase):
    def assert_rotates(self, exc, cooldown):
        """First key fails with ``exc``; the same request is answered by the second key; the first then cools down."""
        self.calls = []
        pool = self.pool("a", "b")
        self.assertEqual(pool.call(self.fn({"a": [exc]})), "ok")
        self.assertEqual(self.calls, ["a", "b"])
        fn = self.fn()
        self.calls.clear()
        self.clock.advance(cooldown - 0.1)
        pool.call(fn)
        self.assertEqual(self.calls, ["b"])  # still cooling
        self.clock.advance(0.1)
        pool.call(fn)
        self.assertEqual(self.calls, ["b", "a"])  # available again

    def test_rate_limit_and_quota(self):
        self.assert_rotates(FakeHttpError(429), 60)
        self.assert_rotates(FakeGenaiError(429, "RESOURCE_EXHAUSTED", "quota exceeded"), 60)

    def test_timeout(self):
        self.assert_rotates(APITimeoutError("timed out"), 15)
        self.assert_rotates(TimeoutError(), 15)

    def test_connection_error(self):
        self.assert_rotates(APIConnectionError("refused"), 15)
        self.assert_rotates(ConnectionResetError(), 15)

    def test_server_errors(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                self.assert_rotates(FakeHttpError(status), 15)
        self.assert_rotates(FakeGenaiError(503, "UNAVAILABLE", "overloaded"), 15)

    def test_unauthorized_and_forbidden_get_a_long_cooldown(self):
        self.assert_rotates(FakeHttpError(401), 3600)
        self.assert_rotates(FakeHttpError(403), 3600)
        self.assert_rotates(FakeGenaiError(400, "INVALID_ARGUMENT", "API_KEY_INVALID"), 3600)

    def test_empty_answer_rotates_without_a_cooldown(self):
        pool = self.pool("a", "b")
        check = lambda result: None if result else EMPTY_ANSWER  # noqa: E731
        self.assertEqual(pool.call(self.fn({"a": [""]}), check), "ok")
        self.assertEqual(self.calls, ["a", "b"])
        pool.call(self.fn(), check)
        self.assertEqual(self.calls, ["a", "b", "a"])  # a was not penalised: the prompt was the problem

    def test_a_check_failure_can_carry_a_retry_after(self):
        pool = self.pool("a", "b")
        check = lambda result: Failure(RATE_LIMITED, 5.0) if result == "429" else None  # noqa: E731
        self.assertEqual(pool.call(self.fn({"a": ["429"]}), check), "ok")
        self.calls.clear()
        fn = self.fn()
        self.clock.advance(4.9)
        pool.call(fn, check)
        self.clock.advance(0.1)
        pool.call(fn, check)
        self.assertEqual(self.calls, ["b", "a"])

    def test_rotation_works_through_several_consecutive_failures(self):
        pool = self.pool("a", "b", "c")
        fn = self.fn({"a": [FakeHttpError(503)], "b": [APITimeoutError("t")]})
        self.assertEqual(pool.call(fn), "ok")
        self.assertEqual(self.calls, ["a", "b", "c"])


class NoRotationTests(PoolTestCase):
    def test_request_errors_are_raised_untouched_and_do_not_cool_the_key(self):
        for exc in (FakeHttpError(400), FakeHttpError(404), FakeHttpError(413), FakeHttpError(422),
                    FakeGenaiError(400, "INVALID_ARGUMENT", "Unable to process input image")):
            with self.subTest(repr(exc)):
                self.calls = []
                pool = self.pool("a", "b", "c")
                with self.assertRaises(Exception) as ctx:
                    pool.call(self.fn({"a": [exc]}))
                self.assertIs(ctx.exception, exc)
                self.assertEqual(self.calls, ["a"])  # no retry on another key
                fn = self.fn()
                for _ in range(3):
                    pool.call(fn)
                self.assertEqual(self.calls, ["a", "b", "c", "a"])  # a was not put on cooldown

    def test_unexpected_exceptions_are_not_swallowed(self):
        for exc in (ValueError("bug"), RuntimeError("boom")):
            with self.subTest(repr(exc)):
                self.calls = []
                with self.assertRaises(type(exc)):
                    self.pool("a", "b").call(self.fn({"a": [exc]}))
                self.assertEqual(self.calls, ["a"])

    def test_no_rotation_means_no_log_line(self):
        with self.assertNoLogs("services.key_pool", "INFO"):
            with self.assertRaises(FakeHttpError):
                self.pool("a", "b").call(self.fn({"a": [FakeHttpError(400)]}))


class AttemptBoundTests(PoolTestCase):
    def test_at_most_max_attempts_keys_are_tried(self):
        pool = self.pool("a", "b", "c", "d", "e")
        script = {k: [FakeHttpError(500, message=f"err-{k}")] for k in "abcde"}
        with self.assertRaises(FakeHttpError) as ctx:
            pool.call(self.fn(script))
        self.assertEqual(self.calls, ["a", "b", "c"])
        self.assertIn("err-c", str(ctx.exception))  # the last failure is what the caller sees

    def test_max_attempts_is_configurable(self):
        pool = self.pool("a", "b", "c", "d")
        with mock.patch.object(config, "KEY_MAX_ATTEMPTS", 2), self.assertRaises(FakeHttpError):
            pool.call(self.fn({k: [FakeHttpError(500)] for k in "abcd"}))
        self.assertEqual(self.calls, ["a", "b"])

    def test_a_key_is_never_tried_twice_for_one_request(self):
        pool = self.pool("a", "b")
        with self.assertRaises(FakeHttpError):
            pool.call(self.fn({"a": [FakeHttpError(429)] * 3, "b": [FakeHttpError(429)] * 3}))
        self.assertEqual(self.calls, ["a", "b"])

    def test_keys_that_are_cooling_do_not_count_as_attempts_but_are_not_tried(self):
        pool = self.pool("a", "b", "c")
        pool.call(self.fn({"a": [FakeHttpError(429)]}))          # a cools down; b answers
        self.calls.clear()
        with self.assertRaises(FakeHttpError):
            pool.call(self.fn({"b": [FakeHttpError(500)], "c": [FakeHttpError(500)]}))
        self.assertEqual(sorted(self.calls), ["b", "c"])         # a was skipped, only two keys were available

    def test_no_new_attempt_starts_after_the_time_budget(self):
        pool = self.pool("a", "b", "c")

        def slow_timeout(key):
            self.calls.append(key)
            self.clock.advance(60)  # each attempt burns a full SDK timeout
            raise APITimeoutError("timed out")

        with self.assertRaises(APITimeoutError):
            pool.call(slow_timeout)
        self.assertEqual(self.calls, ["a", "b"])  # after 120 s >= 90 s budget the third key is not tried

    def test_the_budget_is_configurable(self):
        pool = self.pool("a", "b", "c")

        def slow_timeout(key):
            self.calls.append(key)
            self.clock.advance(60)
            raise APITimeoutError("timed out")

        with mock.patch.object(config, "KEY_RETRY_BUDGET_SECONDS", 500.0), self.assertRaises(APITimeoutError):
            pool.call(slow_timeout)
        self.assertEqual(self.calls, ["a", "b", "c"])

    def test_exhausted_check_failures_return_the_last_result_instead_of_raising(self):
        pool = self.pool("a", "b")
        check = lambda result: Failure(TRANSIENT) if result.startswith("bad") else None  # noqa: E731
        results = {"a": ["bad-a"], "b": ["bad-b"]}
        self.assertEqual(pool.call(self.fn(results), check), "bad-b")
        self.assertEqual(self.calls, ["a", "b"])


class ExhaustionTests(PoolTestCase):
    def test_when_every_key_is_cooling_the_call_fails_at_once_without_sleeping(self):
        pool = self.pool("a", "b")
        with self.assertRaises(FakeHttpError):
            pool.call(self.fn({"a": [FakeHttpError(429)], "b": [FakeHttpError(429)]}))
        self.calls.clear()
        self.clock.advance(20)
        with mock.patch("time.sleep", side_effect=AssertionError("must not sleep")):
            with self.assertRaises(AllKeysCoolingDown) as ctx:
                pool.call(self.fn())
        self.assertEqual(self.calls, [])  # no provider call was made
        self.assertIsInstance(ctx.exception, RuntimeError)  # the router's failover catches it like any failure
        self.assertIn("all 2 test API keys are cooling down", str(ctx.exception))
        self.assertIn("40s", str(ctx.exception))  # 60 s cooldown, 20 s elapsed

    def test_recovers_when_the_first_cooldown_ends(self):
        pool = self.pool("a", "b")
        with self.assertRaises(FakeHttpError):
            pool.call(self.fn({"a": [FakeHttpError(429)], "b": [FakeHttpError(500)]}))  # b only 15 s
        self.clock.advance(15)
        self.assertEqual(pool.call(self.fn()), "ok")
        self.assertEqual(self.calls[-1], "b")

    def test_no_keys_is_a_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "no test API key configured"):
            self.pool().call(self.fn())


class SingleKeyTests(PoolTestCase):
    """One key: a plain pass-through, exactly like the code before rotation existed."""

    def test_failures_propagate_and_the_key_is_never_cooled_down(self):
        pool = self.pool("only")
        for _ in range(3):
            with self.assertRaises(FakeHttpError):
                pool.call(self.fn({"only": [FakeHttpError(429)]}))
        self.assertEqual(self.calls, ["only"] * 3)  # every request still reaches the provider

    def test_the_result_is_returned_unchecked(self):
        check = lambda result: EMPTY_ANSWER  # noqa: E731
        self.assertEqual(self.pool("only").call(self.fn(default=""), check), "")
        self.assertEqual(self.calls, ["only"])

    def test_nothing_is_logged(self):
        with self.assertNoLogs("services.key_pool", "INFO"):
            with self.assertRaises(FakeHttpError):
                self.pool("only").call(self.fn({"only": [FakeHttpError(503)]}))

    def test_a_bad_request_and_a_bug_propagate_too(self):
        for exc in (FakeHttpError(400), ValueError("x")):
            with self.assertRaises(type(exc)):
                self.pool("only").call(self.fn({"only": [exc]}))


class LoggingTests(PoolTestCase):
    def test_one_info_line_per_rotation_naming_only_provider_and_position(self):
        pool = self.pool("SECRET-a", "SECRET-b", "SECRET-c")
        fn = self.fn({"SECRET-a": [FakeHttpError(429)], "SECRET-b": [APITimeoutError("SECRET-b timed out")]})
        with self.assertLogs("services.key_pool", "INFO") as logs:
            pool.call(fn)
        self.assertEqual(len(logs.records), 2)
        self.assertTrue(all(r.levelname == "INFO" for r in logs.records))
        first, second = (r.getMessage() for r in logs.records)
        self.assertIn("test key #1/3", first)
        self.assertIn("rotating to key #2/3", first)
        self.assertIn("rate limited", first)
        self.assertIn("test key #2/3", second)
        self.assertIn("rotating to key #3/3", second)
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_a_successful_first_attempt_logs_nothing(self):
        with self.assertNoLogs("services.key_pool", "INFO"):
            self.pool("a", "b").call(self.fn())

    def test_failing_without_anything_to_rotate_to_logs_no_rotation(self):
        pool = self.pool("a", "b")
        with self.assertRaises(FakeHttpError):
            pool.call(self.fn({"a": [FakeHttpError(429)], "b": [FakeHttpError(429)]}))
        with self.assertNoLogs("services.key_pool", "INFO"):
            with self.assertRaises(AllKeysCoolingDown):
                pool.call(self.fn())

    def test_the_pool_repr_hides_the_keys(self):
        self.assertNotIn("SECRET", repr(self.pool("SECRET-a", "SECRET-b")))


class ConcurrencyTests(PoolTestCase):
    THREADS, REQUESTS = 8, 25

    def run_threads(self, work):
        errors = []
        barrier = threading.Barrier(self.THREADS)

        def target():
            try:
                barrier.wait(timeout=5)
                for _ in range(self.REQUESTS):
                    work()
            except Exception as exc:  # pragma: no cover - only reached when the test fails
                errors.append(exc)

        threads = [threading.Thread(target=target) for _ in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads), "deadlock")
        self.assertEqual(errors, [])

    def test_round_robin_stays_even_under_concurrent_requests(self):
        pool, lock, used = self.pool("a", "b", "c"), threading.Lock(), []

        def fn(key):
            with lock:
                used.append(key)
            return "ok"

        self.run_threads(lambda: pool.call(fn))
        counts = Counter(used)
        self.assertEqual(sum(counts.values()), self.THREADS * self.REQUESTS)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_concurrent_failures_never_break_rotation(self):
        pool, lock, used = self.pool("a", "b", "c"), threading.Lock(), Counter()

        def fn(key):
            with lock:
                used[key] += 1
            if key != "c":
                raise FakeHttpError(429)
            return "ok"

        answers, answers_lock = [], threading.Lock()

        def work():
            result = pool.call(fn)
            with answers_lock:
                answers.append(result)

        self.run_threads(work)
        self.assertEqual(answers, ["ok"] * (self.THREADS * self.REQUESTS))  # every request was served
        # a and b are put on cooldown by their first failure, so each thread reaches them at most once
        self.assertLessEqual(used["a"], self.THREADS)
        self.assertLessEqual(used["b"], self.THREADS)

    def test_exhaustion_is_reported_consistently_under_concurrency(self):
        pool = self.pool("a", "b")
        outcomes, lock = Counter(), threading.Lock()

        def work():
            try:
                pool.call(self.fn(default=FakeHttpError(429)))
            except FakeHttpError:
                kind = "provider error"
            except AllKeysCoolingDown:
                kind = "cooling down"
            with lock:
                outcomes[kind] += 1

        self.run_threads(work)
        self.assertEqual(sum(outcomes.values()), self.THREADS * self.REQUESTS)
        self.assertGreater(outcomes["cooling down"], 0)


class ModuleClockTests(unittest.TestCase):
    def test_pools_without_their_own_clock_use_the_module_clock(self):
        clock = FakeClock()
        with mock.patch.object(key_pool, "clock", clock):
            pool = KeyPool("test", lambda: ["a", "b"])
            calls = []

            def fn(key):
                calls.append(key)
                if key == "a":
                    raise FakeHttpError(429)
                return "ok"

            pool.call(fn)
            pool.call(fn)
            self.assertEqual(calls, ["a", "b", "b"])
            clock.advance(60)
            pool.call(fn)
            self.assertEqual(calls[3:], ["a", "b"])  # a is offered again after its cooldown


if __name__ == "__main__":
    unittest.main()
