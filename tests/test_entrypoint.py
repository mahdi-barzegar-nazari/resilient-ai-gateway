import unittest
from unittest import mock

import config
import main
from services import health_server


class HealthServerTests(unittest.TestCase):
    def test_root_and_healthz_respond(self):
        client = health_server.create_app().test_client()
        root = client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertIn("online", root.get_data(as_text=True))
        health = client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json(), {"status": "ok"})


class MainTests(unittest.TestCase):
    def setUp(self):
        mock.patch("main.setup_logging").start()  # do not reconfigure logging of the whole test run
        self.health = mock.patch("services.health_server.start_in_background").start()
        self.telegram_cls = mock.patch("bot.telegram_handler.TelegramApp").start()
        self.bale_cls = mock.patch("bot.bale_handler.BaleApp").start()
        self.thread_cls = mock.patch("threading.Thread").start()
        self.addCleanup(mock.patch.stopall)

    def test_refuses_to_start_without_any_bot_token(self):
        with mock.patch.object(config, "TELEGRAM_BOT_TOKEN", None), mock.patch.object(config, "BALE_BOT_TOKEN", None):
            self.assertEqual(main.main(), 1)
        self.telegram_cls.assert_not_called()
        self.bale_cls.assert_not_called()

    def test_both_platforms_telegram_on_main_thread_bale_in_background(self):
        self.assertEqual(main.main(), 0)
        self.health.assert_called_once()
        self.thread_cls.assert_called_once()
        self.assertIs(self.thread_cls.call_args.kwargs["target"], self.bale_cls.return_value.run)
        self.assertTrue(self.thread_cls.call_args.kwargs["daemon"])
        self.thread_cls.return_value.start.assert_called_once()
        self.telegram_cls.return_value.run.assert_called_once()

    def test_telegram_only(self):
        with mock.patch.object(config, "BALE_BOT_TOKEN", None):
            main.main()
        self.bale_cls.assert_not_called()
        self.thread_cls.assert_not_called()
        self.telegram_cls.return_value.run.assert_called_once()

    def test_bale_only_runs_on_the_main_thread(self):
        with mock.patch.object(config, "TELEGRAM_BOT_TOKEN", None):
            main.main()
        self.telegram_cls.assert_not_called()
        self.bale_cls.return_value.run.assert_called_once()
        self.thread_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
