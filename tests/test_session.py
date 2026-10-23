import unittest
from unittest import mock

import config
from core.session import PLATFORM_BALE, PLATFORM_TELEGRAM, SETTING_ACTIONS, Session, SessionStore


class SessionTests(unittest.TestCase):
    def test_privacy_first_defaults(self):
        s = Session()
        self.assertFalse(s.memory_on)
        self.assertFalse(s.exam_mode)
        self.assertFalse(s.long_mode)
        self.assertEqual(s.model, config.MODEL_FAST)
        self.assertEqual(s.history, [])

    def test_model_actions(self):
        s = Session()
        s.apply_action("smart")
        self.assertEqual(s.model, config.MODEL_SMART)
        s.apply_action("gemini")
        self.assertEqual(s.model, config.MODEL_GEMINI)
        s.apply_action("fast")
        self.assertEqual(s.model, config.MODEL_FAST)

    def test_toggle_actions(self):
        s = Session()
        for on, off, attr in (("memory_on", "memory_off", "memory_on"),
                              ("exam_on", "exam_off", "exam_mode"),
                              ("long_on", "long_off", "long_mode")):
            s.apply_action(on)
            self.assertTrue(getattr(s, attr), on)
            s.apply_action(off)
            self.assertFalse(getattr(s, attr), off)

    def test_memory_off_wipes_history(self):
        s = Session()
        s.apply_action("memory_on")
        s.remember("q", "a")
        s.apply_action("memory_off")
        self.assertEqual(s.history, [])

    def test_unknown_action_is_rejected(self):
        s = Session()
        self.assertFalse(s.apply_action("bogus"))
        self.assertTrue(all(Session().apply_action(a) for a in SETTING_ACTIONS))

    def test_history_window_keeps_only_recent_messages(self):
        s = Session()
        with mock.patch.object(config, "MAX_HISTORY_MESSAGES", 4):
            for i in range(5):
                s.remember(f"q{i}", f"a{i}")
        self.assertEqual([m["content"] for m in s.history], ["q3", "a3", "q4", "a4"])
        self.assertEqual(s.history[0]["role"], "user")

    def test_clear_history(self):
        s = Session()
        s.remember("q", "a")
        s.clear_history()
        self.assertEqual(s.history, [])


class SessionStoreTests(unittest.TestCase):
    def test_same_key_returns_same_session(self):
        store = SessionStore()
        self.assertIs(store.get(PLATFORM_TELEGRAM, 1), store.get(PLATFORM_TELEGRAM, 1))

    def test_platforms_never_share_state_even_with_the_same_chat_id(self):
        store = SessionStore()
        store.get(PLATFORM_TELEGRAM, 5).apply_action("memory_on")
        self.assertFalse(store.get(PLATFORM_BALE, 5).memory_on)

    def test_chats_are_isolated(self):
        store = SessionStore()
        store.get(PLATFORM_TELEGRAM, 1).apply_action("exam_on")
        self.assertFalse(store.get(PLATFORM_TELEGRAM, 2).exam_mode)


if __name__ == "__main__":
    unittest.main()
