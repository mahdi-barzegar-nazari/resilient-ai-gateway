"""Key rotation at every call site: Groq chat and Whisper, Gemini text/image/PDF, knowledge service, Tavily."""
import importlib
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import requests

import config
from core.errors import ToolError
from core.prompts import FAILOVER_NOTE_TO_GEMINI, FAILOVER_NOTE_TO_GROQ
from services import ai_router as router
from services import audio_service, clients, key_pool
from services import knowledge_service as ks
from services import web_service as web
from services.key_pool import AllKeysCoolingDown, KeyPool
from tests.helpers import FakeClock, FakeGenaiError, FakeHttpError, FakeResponse, pin_rotation_settings

PDF = {"data": b"%PDF-1.5 body", "mime": "application/pdf"}
IMAGE = {"data": b"\xff\xd8", "mime": "image/jpeg"}
SLOW = [FakeHttpError(429)] * 50  # a key that keeps answering 429


def groq_reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class FakeSdk:
    """Per-key fake SDK clients: record every call and play back scripted outcomes (exceptions are raised)."""

    def __init__(self, script=None):
        self.script = {key: list(outcomes) for key, outcomes in (script or {}).items()}
        self.calls = []  # (key, endpoint, setting, kwargs)
        self.made = []   # (key, setting, client)

    def keys_called(self):
        return [call[0] for call in self.calls]

    def _play(self, key, endpoint, setting, kwargs, default):
        self.calls.append((key, endpoint, setting, kwargs))
        outcomes = self.script.get(key)
        outcome = outcomes.pop(0) if outcomes else default
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def groq(self, key, max_retries):
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kw: self._play(key, "chat", None, kw, groq_reply(f"groq:{key}")))),
            audio=SimpleNamespace(transcriptions=SimpleNamespace(
                create=lambda **kw: self._play(key, "audio", None, kw, SimpleNamespace(text=f"heard:{key}")))),
        )
        self.made.append((key, max_retries, client))
        return client

    def gemini(self, key, timeout_ms):
        client = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda **kw: self._play(key, "gemini", timeout_ms, kw, SimpleNamespace(text=f"gemini:{key}"))))
        self.made.append((key, timeout_ms, client))
        return client


class RotationTestCase(unittest.TestCase):
    """Real facades over fake SDK clients, a fake clock, and a ``time.sleep`` that fails the test if reached."""

    def setUp(self):
        self.clock = FakeClock()
        pin_rotation_settings(self)
        for patcher in (mock.patch.object(key_pool, "clock", self.clock),
                        mock.patch("time.sleep", side_effect=AssertionError("key rotation must never sleep"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, groq=(), gemini=(), groq_script=None, gemini_script=None):
        self.groq_sdk, self.gemini_sdk = FakeSdk(groq_script), FakeSdk(gemini_script)
        groq, gemini = list(groq), list(gemini)
        pool = KeyPool("gemini", lambda: gemini)
        built = {
            "groq_client": clients.build_groq_client(groq, bool(gemini), make=self.groq_sdk.groq) if groq else None,
            "gemini_client": clients.build_gemini_client(gemini, 60000, pool, make=self.gemini_sdk.gemini) if gemini else None,
            "gemini_pdf_client": clients.build_gemini_client(gemini, 180000, pool, make=self.gemini_sdk.gemini) if gemini else None,
        }
        for name, value in built.items():
            patcher = mock.patch.object(clients, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ask(self, model=None, **kwargs):
        kwargs.setdefault("use_web", False)
        return router.process_ai_query("explain heaps", model_name=model or config.MODEL_FAST, **kwargs)


class ClientConstructionTests(unittest.TestCase):
    def test_one_groq_key_gives_the_plain_sdk_client_built_as_before(self):
        sdk = FakeSdk()
        client = clients.build_groq_client(["only"], failover_available=True, make=sdk.groq)
        self.assertIs(client, sdk.made[0][2])
        self.assertEqual(sdk.made[0][:2], ("only", 0))  # fail fast when Gemini can take over

        sdk = FakeSdk()
        clients.build_groq_client(["only"], failover_available=False, make=sdk.groq)
        self.assertEqual(sdk.made[0][:2], ("only", 2))  # the SDK's own retries stay when there is no failover

    def test_several_groq_keys_get_one_client_each_with_sdk_retries_off(self):
        for failover in (True, False):
            sdk = FakeSdk()
            client = clients.build_groq_client(["k1", "k2", "k3"], failover, make=sdk.groq)
            self.assertEqual([(key, retries) for key, retries, _ in sdk.made], [("k1", 0), ("k2", 0), ("k3", 0)])
            self.assertTrue(client)  # callers test the client for truthiness
            self.assertTrue(callable(client.chat.completions.create))
            self.assertTrue(callable(client.audio.transcriptions.create))

    def test_one_gemini_key_gives_the_plain_sdk_client_with_its_timeout(self):
        sdk = FakeSdk()
        client = clients.build_gemini_client(["only"], 180000, make=sdk.gemini)
        self.assertIs(client, sdk.made[0][2])
        self.assertEqual(sdk.made[0][:2], ("only", 180000))

    def test_several_gemini_keys_get_one_client_each_with_the_requested_timeout(self):
        sdk = FakeSdk()
        client = clients.build_gemini_client(["m1", "m2"], 60000, make=sdk.gemini)
        self.assertEqual([(key, timeout) for key, timeout, _ in sdk.made], [("m1", 60000), ("m2", 60000)])
        self.assertTrue(callable(client.models.generate_content))

    def test_plural_only_configuration_builds_the_module_level_clients(self):
        try:
            with mock.patch.object(config, "GROQ_API_KEY", None), mock.patch.object(config, "GROQ_API_KEYS", ["x1", "x2"]), \
                    mock.patch.object(config, "GEMINI_API_KEY", None), mock.patch.object(config, "GEMINI_API_KEYS", ["y1", "y2"]):
                reloaded = importlib.reload(clients)
                self.assertTrue(reloaded.groq_client)
                self.assertTrue(reloaded.gemini_client)
                self.assertTrue(reloaded.gemini_pdf_client)
            with mock.patch.object(config, "GROQ_API_KEY", None), mock.patch.object(config, "GROQ_API_KEYS", []), \
                    mock.patch.object(config, "GEMINI_API_KEY", None), mock.patch.object(config, "GEMINI_API_KEYS", []):
                reloaded = importlib.reload(clients)
                self.assertIsNone(reloaded.groq_client)
                self.assertIsNone(reloaded.gemini_client)
                self.assertIsNone(reloaded.gemini_pdf_client)
        finally:
            importlib.reload(clients)  # restore the clients built from the test environment


class ModuleWiringTests(RotationTestCase):
    def test_the_module_level_gemini_clients_share_one_cooldown_pool(self):
        from google import genai
        sdk = FakeSdk({"y1": [FakeGenaiError(429, "RESOURCE_EXHAUSTED", "quota")]})

        def fake_client(api_key=None, http_options=None, **kwargs):
            return sdk.gemini(api_key, http_options.timeout)

        try:
            with mock.patch.object(config, "GEMINI_API_KEY", None), mock.patch.object(config, "GEMINI_API_KEYS", ["y1", "y2"]), \
                    mock.patch.object(genai, "Client", fake_client):
                reloaded = importlib.reload(clients)
                reloaded.gemini_client.models.generate_content(model="m", contents=[])      # y1 is rate limited
                reloaded.gemini_pdf_client.models.generate_content(model="m", contents=[])  # must skip y1 too
        finally:
            importlib.reload(clients)
        self.assertEqual([(k, t) for k, _, t, _ in sdk.calls], [("y1", 60000), ("y2", 60000), ("y2", 180000)])


class GroqCallSiteTests(RotationTestCase):
    def test_chat_rotates_to_the_next_key_on_429(self):
        self.install(groq=["g1", "g2", "g3"], groq_script={"g1": [FakeHttpError(429)]})
        self.assertEqual(router.call_groq("q", "sys", config.MODEL_FAST, []), "groq:g2")
        self.assertEqual(self.groq_sdk.keys_called(), ["g1", "g2"])
        kwargs = self.groq_sdk.calls[1][3]  # the request is replayed unchanged
        self.assertEqual((kwargs["model"], kwargs["max_tokens"]), (config.MODEL_FAST, 800))

    def test_chat_spreads_requests_round_robin(self):
        self.install(groq=["g1", "g2", "g3"])
        answers = [router.call_groq("q", "sys", config.MODEL_FAST, []) for _ in range(4)]
        self.assertEqual(answers, ["groq:g1", "groq:g2", "groq:g3", "groq:g1"])

    def test_whisper_rotates_on_timeout_and_replays_the_same_audio(self):
        self.install(groq=["g1", "g2"], groq_script={"g1": [type("APITimeoutError", (Exception,), {})("timed out")]})
        with mock.patch.object(config, "WHISPER_LANGUAGE", "fa"):
            self.assertEqual(audio_service.transcribe_audio(b"ogg-bytes", "audio.ogg"), "heard:g2")
        first, second = self.groq_sdk.calls[0][3], self.groq_sdk.calls[1][3]
        self.assertEqual(first, second)
        self.assertEqual(second["file"], ("audio.ogg", b"ogg-bytes"))
        self.assertEqual(second["language"], "fa")

    def test_silence_is_a_valid_transcript_and_is_not_retried(self):
        self.install(groq=["g1", "g2"], groq_script={"g1": [SimpleNamespace(text="")]})
        self.assertEqual(audio_service.transcribe_audio(b"x", "audio.ogg"), "")
        self.assertEqual(self.groq_sdk.keys_called(), ["g1"])

    def test_chat_and_whisper_share_one_cooldown_per_key(self):
        self.install(groq=["g1", "g2"], groq_script={"g1": [FakeHttpError(429)]})
        audio_service.transcribe_audio(b"x", "audio.ogg")           # g1 is rate limited, g2 answers
        self.assertEqual(router.call_groq("q", "sys", config.MODEL_FAST, []), "groq:g2")
        self.assertEqual(self.groq_sdk.keys_called(), ["g1", "g2", "g2"])

    def test_empty_answer_is_retried_on_the_next_key(self):
        for empty in (groq_reply("   "), groq_reply(None), SimpleNamespace(choices=[])):
            with self.subTest(empty=empty):
                self.install(groq=["g1", "g2"], groq_script={"g1": [empty]})
                self.assertEqual(router.call_groq("q", "sys", config.MODEL_FAST, []), "groq:g2")

    def test_when_every_key_answers_empty_call_groq_raises_its_own_error(self):
        self.install(groq=["g1", "g2", "g3"], groq_script={k: [groq_reply("")] for k in ("g1", "g2", "g3")})
        with self.assertRaisesRegex(RuntimeError, "پاسخ خالی از Groq"):
            router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.assertEqual(self.groq_sdk.keys_called(), ["g1", "g2", "g3"])

    def test_a_bad_request_is_raised_and_not_replayed(self):
        bad = FakeHttpError(400, message="Bad Request: messages must not be empty")
        self.install(groq=["g1", "g2"], groq_script={"g1": [bad]})
        with self.assertRaises(FakeHttpError) as ctx:
            router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.assertIs(ctx.exception, bad)
        self.assertEqual(self.groq_sdk.keys_called(), ["g1"])

    def test_unauthorized_key_rests_for_an_hour(self):
        self.install(groq=["g1", "g2"], groq_script={"g1": [FakeHttpError(401)]})
        router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.groq_sdk.calls.clear()
        self.clock.advance(3599)
        for _ in range(3):
            router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.assertEqual(set(self.groq_sdk.keys_called()), {"g2"})
        self.clock.advance(1)
        self.groq_sdk.calls.clear()
        router.call_groq("q", "sys", config.MODEL_FAST, [])
        router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.assertIn("g1", self.groq_sdk.keys_called())

    def test_retry_after_from_the_response_headers_is_honoured(self):
        self.install(groq=["g1", "g2"], groq_script={"g1": [FakeHttpError(429, {"retry-after": "7"})]})
        router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.groq_sdk.calls.clear()
        self.clock.advance(6.9)
        router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.clock.advance(0.1)
        router.call_groq("q", "sys", config.MODEL_FAST, [])
        self.assertEqual(self.groq_sdk.keys_called(), ["g2", "g1"])


class GeminiCallSiteTests(RotationTestCase):
    def test_text_image_and_pdf_all_rotate_and_use_the_right_timeout(self):
        cases = (("text", None, 60000), ("image", IMAGE, 60000), ("pdf", PDF, 180000))
        for name, image, timeout in cases:
            with self.subTest(name):
                self.install(gemini=["m1", "m2"], gemini_script={"m1": [FakeGenaiError(429, "RESOURCE_EXHAUSTED", "quota")]})
                self.assertEqual(router.call_gemini("q", "sys", [], image=image), "gemini:m2")
                self.assertEqual([(k, t) for k, _, t, _ in self.gemini_sdk.calls], [("m1", timeout), ("m2", timeout)])

    def test_the_long_timeout_pdf_client_and_the_normal_client_share_cooldowns(self):
        self.install(gemini=["m1", "m2"], gemini_script={"m1": [FakeGenaiError(429, "RESOURCE_EXHAUSTED")]})
        router.call_gemini("q", "sys", [])                 # normal client: m1 is rate limited
        router.call_gemini("q", "sys", [], image=PDF)      # PDF client must skip m1 as well
        self.assertEqual([(k, t) for k, _, t, _ in self.gemini_sdk.calls], [("m1", 60000), ("m2", 60000), ("m2", 180000)])

        self.install(gemini=["m1", "m2"], gemini_script={"m1": [FakeGenaiError(503, "UNAVAILABLE")]})
        router.call_gemini("q", "sys", [], image=PDF)      # learned through the PDF client ...
        router.call_gemini("q", "sys", [])                 # ... applies to the normal one
        self.assertEqual([(k, t) for k, _, t, _ in self.gemini_sdk.calls], [("m1", 180000), ("m2", 180000), ("m2", 60000)])

    def test_each_trigger_rotates(self):
        triggers = {
            "timeout": type("ReadTimeout", (Exception,), {})("t"),
            "connection": ConnectionError("refused"),
            "overloaded": FakeGenaiError(503, "UNAVAILABLE", "The model is overloaded"),
            "internal": FakeGenaiError(500, "INTERNAL", "boom"),
            "invalid key": FakeGenaiError(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
            "permission": FakeGenaiError(403, "PERMISSION_DENIED", "denied"),
        }
        for name, exc in triggers.items():
            with self.subTest(name):
                self.install(gemini=["m1", "m2"], gemini_script={"m1": [exc]})
                self.assertEqual(router.call_gemini("q", "sys", []), "gemini:m2")

    def test_empty_answer_is_retried_on_the_next_key(self):
        self.install(gemini=["m1", "m2"], gemini_script={"m1": [SimpleNamespace(text="")]})
        self.assertEqual(router.call_gemini("q", "sys", []), "gemini:m2")

    def test_when_every_key_answers_empty_call_gemini_raises_its_own_error(self):
        self.install(gemini=["m1", "m2"], gemini_script={k: [SimpleNamespace(text=None)] for k in ("m1", "m2")})
        with self.assertRaisesRegex(RuntimeError, "پاسخ خالی از Gemini"):
            router.call_gemini("q", "sys", [])

    def test_a_bad_request_is_not_replayed(self):
        bad = FakeGenaiError(400, "INVALID_ARGUMENT", "Unable to process input image")
        self.install(gemini=["m1", "m2"], gemini_script={"m1": [bad]})
        with self.assertRaises(FakeGenaiError):
            router.call_gemini("q", "sys", [], image=IMAGE)
        self.assertEqual(self.gemini_sdk.keys_called(), ["m1"])


class FailoverBetweenProvidersTests(RotationTestCase):
    def test_all_groq_keys_exhausted_hands_over_to_gemini(self):
        self.install(groq=["g1", "g2"], gemini=["m1"], groq_script={"g1": SLOW, "g2": SLOW})
        self.assertEqual(self.ask(), ("gemini:m1", FAILOVER_NOTE_TO_GEMINI))
        self.assertEqual(self.groq_sdk.keys_called(), ["g1", "g2"])  # both keys were tried first

    def test_following_requests_skip_groq_entirely_and_never_sleep(self):
        self.install(groq=["g1", "g2"], gemini=["m1"], groq_script={"g1": SLOW, "g2": SLOW})
        self.ask()
        calls_before = len(self.groq_sdk.calls)
        for _ in range(3):
            self.assertEqual(self.ask(), ("gemini:m1", FAILOVER_NOTE_TO_GEMINI))
        self.assertEqual(len(self.groq_sdk.calls), calls_before)  # Groq was not even contacted (time.sleep is booby-trapped)

    def test_groq_is_tried_again_once_the_cooldowns_end(self):
        self.install(groq=["g1", "g2"], gemini=["m1"], groq_script={"g1": [FakeHttpError(429)], "g2": [FakeHttpError(429)]})
        self.ask()
        self.clock.advance(60)
        self.assertEqual(self.ask(), ("groq:g1", ""))

    def test_gemini_first_falls_back_to_groq_and_then_skips_gemini(self):
        self.install(groq=["g1"], gemini=["m1", "m2"],
                     gemini_script={"m1": [FakeGenaiError(503, "UNAVAILABLE")] * 9, "m2": [FakeGenaiError(503, "UNAVAILABLE")] * 9})
        self.assertEqual(self.ask(model=config.MODEL_GEMINI), ("groq:g1", FAILOVER_NOTE_TO_GROQ))
        gemini_calls = len(self.gemini_sdk.calls)
        self.assertEqual(self.ask(model=config.MODEL_GEMINI), ("groq:g1", FAILOVER_NOTE_TO_GROQ))
        self.assertEqual(len(self.gemini_sdk.calls), gemini_calls)

    def test_exam_mode_still_suppresses_the_failover_note(self):
        self.install(groq=["g1", "g2"], gemini=["m1"], groq_script={"g1": SLOW, "g2": SLOW})
        self.assertEqual(self.ask(is_exam=True), ("gemini:m1", ""))

    def test_both_providers_exhausted_names_both_causes_and_hides_the_keys(self):
        self.install(groq=["g1-SECRET", "g2-SECRET"], gemini=["m1-SECRET", "m2-SECRET"],
                     groq_script={"g1-SECRET": SLOW, "g2-SECRET": SLOW},
                     gemini_script={"m1-SECRET": SLOW, "m2-SECRET": SLOW})
        with self.assertRaises(RuntimeError) as first:
            self.ask()
        self.assertIn("Groq (", str(first.exception))
        self.assertIn("Gemini (", str(first.exception))
        self.assertIn("429", str(first.exception))
        with self.assertRaises(RuntimeError) as second:  # now both are cooling down: immediate, still readable
            self.ask()
        self.assertIn("cooling down", str(second.exception))
        for message in (str(first.exception), str(second.exception)):
            self.assertNotIn("SECRET", message)

    def test_images_have_no_failover_even_when_every_gemini_key_cools_down(self):
        self.install(groq=["g1", "g2"], gemini=["m1", "m2"], gemini_script={"m1": SLOW, "m2": SLOW})
        with self.assertRaises(FakeHttpError):
            self.ask(image=IMAGE)
        with self.assertRaises(AllKeysCoolingDown):
            self.ask(image=IMAGE)
        self.assertEqual(self.groq_sdk.calls, [])

    def test_single_keys_behave_exactly_as_before_rotation_existed(self):
        self.install(groq=["g"], gemini=["m"], groq_script={"g": [FakeHttpError(429)] * 5})
        self.assertEqual(self.ask(), ("gemini:m", FAILOVER_NOTE_TO_GEMINI))
        self.assertEqual(self.ask(), ("gemini:m", FAILOVER_NOTE_TO_GEMINI))
        self.assertEqual(self.groq_sdk.keys_called(), ["g", "g"])  # no cooldown: every request still tries Groq
        self.assertEqual(self.groq_sdk.made[0][:2], ("g", 0))      # and the SDK client is the plain one


class KnowledgeServiceTests(RotationTestCase):
    def test_arxiv_full_rotates_when_a_gemini_key_is_rate_limited(self):
        self.install(gemini=["m1", "m2"], gemini_script={"m1": [FakeGenaiError(429, "RESOURCE_EXHAUSTED", "quota")]})
        pdf = FakeResponse(content=b"%PDF-1.5 " + b"x" * 500)
        with mock.patch("requests.get", return_value=pdf):
            out = ks.tool_arxiv_full("1706.03762")
        self.assertEqual(out, "📄 ArXiv:1706.03762\n\ngemini:m2")
        self.assertEqual([(k, t) for k, _, t, _ in self.gemini_sdk.calls], [("m1", 180000), ("m2", 180000)])

    def test_arxiv_full_with_every_key_cooling_is_a_normal_failure_not_a_hang(self):
        self.install(gemini=["m1", "m2"], gemini_script={"m1": SLOW, "m2": SLOW})
        pdf = FakeResponse(content=b"%PDF-1.5 " + b"x" * 500)
        with mock.patch("requests.get", return_value=pdf):
            with self.assertRaises(FakeHttpError):
                ks.tool_arxiv_full("1706.03762")
            with self.assertRaises(AllKeysCoolingDown):
                ks.tool_arxiv_full("1706.03762")


class ConcurrentCallsTests(RotationTestCase):
    def test_threads_share_the_pool_safely(self):
        self.install(groq=["g1", "g2", "g3"], groq_script={"g1": SLOW * 4})
        answers, errors, lock = [], [], threading.Lock()
        barrier = threading.Barrier(4)

        def work():
            try:
                barrier.wait(timeout=5)
                for _ in range(10):
                    text = router.call_groq("q", "sys", config.MODEL_FAST, [])
                    with lock:
                        answers.append(text)
            except Exception as exc:  # pragma: no cover - only reached when the test fails
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(answers), 40)
        self.assertNotIn("groq:g1", answers)  # the rate-limited key never answered
        self.assertLessEqual(self.groq_sdk.keys_called().count("g1"), 4)  # at most one probe per thread


# ---------------------------------------------------------------- Tavily
def tavily_response(status=200, data=None, headers=None):
    response = FakeResponse(data if data is not None else {}, status=status)
    response.headers = headers or {}
    return response


class TavilyTestCase(RotationTestCase):
    def setUp(self):
        super().setUp()
        self.posts = []
        self.script = {}
        for patcher in (
            mock.patch.object(config, "TAVILY_API_KEY", "tav-1"),
            mock.patch.object(config, "TAVILY_API_KEYS", ["tav-2", "tav-3"]),
            mock.patch("requests.post", side_effect=self.fake_post),
            mock.patch("requests.get", side_effect=RuntimeError("RSS down")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fresh_pool()

    def fresh_pool(self):
        pool = KeyPool("tavily", lambda: config.api_keys("tavily"), clock=self.clock)
        patcher = mock.patch.object(web, "tavily_pool", pool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_post(self, url, **kwargs):
        key = kwargs["json"]["api_key"]
        self.posts.append((key, url, kwargs))
        outcomes = self.script.get(key)
        outcome = outcomes.pop(0) if outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        if url.endswith("/search"):
            return tavily_response(data={"answer": f"answer via {key}", "results": []})
        return tavily_response(data={"results": [{"url": "https://example.com/a", "raw_content": f"page text via {key}. " * 20}]})

    def keys_posted(self):
        return [post[0] for post in self.posts]


class TavilyRotationTests(TavilyTestCase):
    def test_search_rotates_on_every_trigger(self):
        triggers = {
            "429 + Retry-After": tavily_response(429, headers={"Retry-After": "5"}),
            "plan quota (432)": tavily_response(432),
            "pay-as-you-go quota (433)": tavily_response(433),
            "500": tavily_response(500),
            "503": tavily_response(503),
            "401": tavily_response(401),
            "403": tavily_response(403),
            "timeout": requests.Timeout("timed out"),
            "connection error": requests.ConnectionError("refused"),
        }
        for name, outcome in triggers.items():
            with self.subTest(name):
                self.posts, self.script = [], {"tav-1": [outcome]}
                self.fresh_pool()
                out = web.fetch_web_context("خبر امروز")
                self.assertIn("answer via tav-2", out)
                self.assertEqual(self.keys_posted(), ["tav-1", "tav-2"])

    def test_extract_sends_the_rotated_key_in_both_header_and_body(self):
        self.script = {"tav-1": [tavily_response(401)]}
        pages = web.extract_link_contents(["https://example.com/a"])
        self.assertIn("page text via tav-2", pages[0][1])
        (key1, _, kw1), (key2, url2, kw2) = self.posts
        self.assertEqual((key1, kw1["headers"]["Authorization"]), ("tav-1", "Bearer tav-1"))
        self.assertEqual((key2, kw2["headers"]["Authorization"]), ("tav-2", "Bearer tav-2"))
        self.assertEqual(url2, "https://api.tavily.com/extract")
        self.assertEqual(kw2["timeout"], 30)

    def test_search_request_shape_is_unchanged(self):
        web.fetch_web_context("قیمت دلار")
        key, url, kwargs = self.posts[0]
        self.assertEqual(url, "https://api.tavily.com/search")
        self.assertEqual(kwargs["timeout"], 7)
        self.assertNotIn("headers", kwargs)
        self.assertEqual(kwargs["json"], {"api_key": key, "query": "قیمت دلار", "search_depth": "basic", "max_results": 4})

    def test_unauthorized_key_rests_for_an_hour(self):
        self.script = {"tav-1": [tavily_response(401)]}
        web.extract_link_contents(["https://example.com/a"])
        self.posts.clear()
        self.clock.advance(3599)
        for _ in range(4):
            web.extract_link_contents(["https://example.com/a"])
        self.assertNotIn("tav-1", self.keys_posted())
        self.clock.advance(1)
        self.posts.clear()
        for _ in range(3):
            web.extract_link_contents(["https://example.com/a"])
        self.assertIn("tav-1", self.keys_posted())

    def test_bad_request_is_not_rotated(self):
        self.script = {"tav-1": [tavily_response(400)]}
        with mock.patch.object(web, "log"):  # the search path just falls back to RSS
            self.assertEqual(web.fetch_web_context("خبر"), "")
        self.assertEqual(self.keys_posted(), ["tav-1"])

        self.posts, self.script = [], {"tav-2": [tavily_response(400)]}
        with self.assertRaises(RuntimeError):  # FakeResponse.raise_for_status -> "HTTP 400"
            web.extract_link_contents(["https://example.com/a"])
        self.assertEqual(self.keys_posted(), ["tav-2"])

    def test_exhausted_keys_keep_the_old_semantics_for_every_caller(self):
        self.script = {k: [tavily_response(429)] * 9 for k in ("tav-1", "tav-2", "tav-3")}
        rss = b"<rss><channel><item><title>Headline</title></item></channel></rss>"
        with mock.patch("requests.get", return_value=FakeResponse(content=rss)):
            self.assertEqual(web.fetch_web_context("خبر"), "• خبر: Headline")  # search: RSS fallback
            self.assertEqual(self.keys_posted(), ["tav-1", "tav-2", "tav-3"])
            self.posts.clear()
            self.assertEqual(web.fetch_web_context("خبر"), "• خبر: Headline")
            self.assertEqual(self.posts, [])  # every key is cooling down: Tavily is not contacted at all
        self.assertEqual(web.prepare_link_prompt("https://example.com/a"), (None, None, web.LINK_ERROR_TEXT))
        with self.assertRaises(ToolError) as ctx:
            web.tool_read("https://example.com/a")
        self.assertEqual(str(ctx.exception), web.LINK_ERROR_TEXT)
        self.assertEqual(self.posts, [])

    def test_max_attempts_bounds_the_keys_tried_per_request(self):
        self.script = {k: [tavily_response(500)] * 9 for k in ("tav-1", "tav-2", "tav-3")}
        with mock.patch.object(config, "KEY_MAX_ATTEMPTS", 2), mock.patch("requests.get", side_effect=RuntimeError("x")):
            web.fetch_web_context("خبر")
        self.assertEqual(self.keys_posted(), ["tav-1", "tav-2"])

    def test_requests_are_spread_round_robin(self):
        for _ in range(4):
            web.fetch_web_context("خبر")
        self.assertEqual(self.keys_posted(), ["tav-1", "tav-2", "tav-3", "tav-1"])

    def test_a_list_alone_is_enough(self):
        with mock.patch.object(config, "TAVILY_API_KEY", None):
            self.assertIn("answer via tav-2", web.fetch_web_context("خبر"))
            prompt, _, error = web.prepare_link_prompt("https://example.com/a")
            self.assertIsNone(error)
            self.assertIn("page text via", prompt)
            self.assertIn("page text via", web.tool_read("https://example.com/a"))

    def test_no_keys_at_all_still_means_no_tavily(self):
        with mock.patch.object(config, "TAVILY_API_KEY", None), mock.patch.object(config, "TAVILY_API_KEYS", []):
            self.assertEqual(web.fetch_web_context("خبر"), "")
            self.assertEqual(web.prepare_link_prompt("https://example.com"), (None, None, None))
            with self.assertRaises(ToolError):
                web.tool_read("https://example.com")
        self.assertEqual(self.posts, [])


class TavilySingleKeyTests(TavilyTestCase):
    """Exactly one key: no rotation, no cooldown, same behaviour as before."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(config, "TAVILY_API_KEY", "only"), mock.patch.object(config, "TAVILY_API_KEYS", [])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_rate_limited_key_is_still_used_for_the_next_request(self):
        self.script = {"only": [tavily_response(429)] * 3}
        for _ in range(2):
            self.assertEqual(web.fetch_web_context("خبر"), "")  # 429 -> RSS fallback (which is down here)
        self.assertEqual(self.keys_posted(), ["only", "only"])

    def test_a_timeout_propagates_to_the_caller(self):
        self.script = {"only": [requests.Timeout("t")]}
        with self.assertRaises(requests.Timeout):
            web.extract_link_contents(["https://example.com/a"])

    def test_a_duplicate_key_in_both_variables_is_still_a_single_key(self):
        with mock.patch.object(config, "TAVILY_API_KEYS", ["only", " only "]):
            self.script = {"only": [tavily_response(429)] * 3}
            web.fetch_web_context("خبر")
            web.fetch_web_context("خبر")
        self.assertEqual(self.keys_posted(), ["only", "only"])


if __name__ == "__main__":
    unittest.main()
