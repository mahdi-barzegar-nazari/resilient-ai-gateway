import sys
import threading
import time
import unittest
from collections import deque
from unittest import mock

import config
from core import ratelimit
from core.ratelimit import Decision, RateLimiter, check_rate_limit, is_metered_tool
from services.tools import TOOLS
from tests.helpers import FakeClock
from tests.test_key_pool import ReloadedConfig

TG, BALE = "tg", "bale"


class YieldingDeque(deque):
    """A deque that lets other threads run every time its length is read.

    The limiter reads the length between "is there a free slot?" and "take it", so without its lock this
    makes a lost update certain instead of a matter of luck.
    """

    def __len__(self):
        time.sleep(0.0001)
        return super().__len__()


class LimiterTestCase(unittest.TestCase):
    """A fresh limiter on a fake clock, with a small limit so every boundary is easy to reach."""

    MAX = 3
    WINDOW = 10.0

    def setUp(self):
        for name, value in (("RATE_LIMIT_MAX", self.MAX), ("RATE_LIMIT_WINDOW_SECONDS", self.WINDOW)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        self.limiter = RateLimiter(clock=self.clock)

    def hit(self, user=1, platform=TG):
        return self.limiter.check(platform, user)

    def burst(self, count, user=1, platform=TG):
        return [self.hit(user, platform) for _ in range(count)]

    def entry(self, user=1, platform=TG):
        return self.limiter._entries[(platform, user)]


class LimitBoundaryTests(LimiterTestCase):
    def test_exactly_the_limit_is_allowed_and_the_next_request_is_not(self):
        decisions = self.burst(self.MAX + 1)
        self.assertTrue(all(d.allowed for d in decisions[:self.MAX]))
        self.assertFalse(decisions[self.MAX].allowed)

    def test_a_limit_of_one(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", 1):
            self.assertTrue(self.hit().allowed)
            self.assertFalse(self.hit().allowed)

    def test_rejected_requests_are_not_counted(self):
        self.burst(self.MAX)
        self.clock.advance(5)
        self.burst(50)  # hammering while blocked must not extend the block
        self.clock.advance(5)  # the three accepted requests are now exactly one window old
        self.assertEqual([d.allowed for d in self.burst(self.MAX + 1)], [True] * self.MAX + [False])

    def test_a_user_never_holds_more_timestamps_than_the_limit(self):
        self.burst(500)
        self.assertEqual(len(self.entry().hits), self.MAX)

    def test_retry_after_counts_down_and_rounds_up(self):
        self.burst(self.MAX)  # all at t=1000
        for elapsed, expected in ((0, 10), (3.0, 7), (3.2, 7), (9.5, 1), (9.999, 1)):
            with self.subTest(elapsed=elapsed):
                self.clock.now = 1000.0 + elapsed
                decision = self.hit()
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.retry_after, expected)

    def test_waiting_retry_after_seconds_is_always_enough(self):
        for offset in (0, 0.3, 2.5, 4.9, 7.01, 9.99):
            with self.subTest(offset=offset):
                limiter = RateLimiter(clock=FakeClock())
                limiter.check(TG, 1)
                limiter._clock.advance(offset)
                limiter.check(TG, 1)
                limiter.check(TG, 1)
                denied = limiter.check(TG, 1)
                self.assertFalse(denied.allowed)
                self.assertGreaterEqual(denied.retry_after, 1)
                limiter._clock.advance(denied.retry_after)
                self.assertTrue(limiter.check(TG, 1).allowed)

    def test_an_allowed_decision_carries_no_notice_and_no_wait(self):
        allowed = self.hit()
        self.assertEqual(allowed, Decision(allowed=True))
        self.assertFalse(allowed.notify)
        self.assertEqual(allowed.retry_after, 0)

    def test_lowering_the_limit_at_runtime_waits_for_enough_slots_to_free_up(self):
        self.burst(3)       # t=1000 x3 under a limit of 3
        self.clock.advance(1)
        self.hit()          # denied; nothing recorded
        with mock.patch.object(config, "RATE_LIMIT_MAX", 2):
            decision = self.hit()
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.retry_after, 9)  # three hits are recorded and the limit is two


class WindowExpiryTests(LimiterTestCase):
    def test_blocked_until_exactly_one_window_after_the_oldest_request(self):
        self.burst(self.MAX)
        self.clock.advance(self.WINDOW - 0.001)
        self.assertFalse(self.hit().allowed)
        self.clock.advance(0.001)
        self.assertTrue(self.hit().allowed)

    def test_the_window_slides_instead_of_resetting(self):
        self.hit()                  # t=0
        self.clock.advance(6)
        self.burst(2)               # t=6, t=6 -> limit reached
        self.clock.advance(3)       # t=9
        self.assertFalse(self.hit().allowed)
        self.clock.advance(1)       # t=10: only the t=0 request expired, so exactly one slot is free
        self.assertTrue(self.hit().allowed)
        self.assertFalse(self.hit().allowed)
        self.clock.advance(6)       # t=16: the two t=6 requests expired
        self.assertEqual([d.allowed for d in self.burst(3)], [True, True, False])

    def test_never_more_than_the_limit_inside_any_window(self):
        accepted = []
        for _ in range(400):
            if self.hit().allowed:
                accepted.append(self.clock.now)
            self.clock.advance(0.5)
        self.assertGreater(len(accepted), 3 * 15)  # it keeps serving the user, it does not lock them out
        for index in range(len(accepted) - self.MAX):
            self.assertGreaterEqual(accepted[index + self.MAX] - accepted[index], self.WINDOW)

    def test_a_quiet_user_starts_from_zero_again(self):
        self.burst(self.MAX)
        self.clock.advance(self.WINDOW * 5)
        self.assertEqual([d.allowed for d in self.burst(self.MAX + 1)], [True] * self.MAX + [False])

    def test_the_window_length_comes_from_config_at_call_time(self):
        self.burst(self.MAX)
        self.clock.advance(4)
        self.assertFalse(self.hit().allowed)
        with mock.patch.object(config, "RATE_LIMIT_WINDOW_SECONDS", 3.0):
            self.assertTrue(self.hit().allowed)


class IsolationTests(LimiterTestCase):
    def test_users_have_separate_buckets(self):
        self.burst(self.MAX, user=1)
        self.assertFalse(self.hit(user=1).allowed)
        self.assertEqual([d.allowed for d in self.burst(self.MAX, user=2)], [True] * self.MAX)

    def test_the_same_id_on_telegram_and_bale_never_shares_a_bucket(self):
        self.burst(self.MAX, user=42, platform=TG)
        self.assertFalse(self.hit(user=42, platform=TG).allowed)
        self.assertEqual([d.allowed for d in self.burst(self.MAX, user=42, platform=BALE)], [True] * self.MAX)
        self.assertFalse(self.hit(user=42, platform=BALE).allowed)

    def test_notices_are_tracked_per_user_and_per_platform(self):
        for user, platform in ((1, TG), (2, TG), (1, BALE)):
            self.burst(self.MAX, user, platform)
        notices = [self.hit(user, platform).notify for user, platform in ((1, TG), (2, TG), (1, BALE))]
        self.assertEqual(notices, [True, True, True])


class DisabledTests(LimiterTestCase):
    def test_a_limit_of_zero_disables_everything(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", 0):
            decisions = self.burst(1000)
        self.assertTrue(all(d == Decision(allowed=True) for d in decisions))

    def test_a_disabled_limiter_stores_nothing(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", 0):
            for user in range(100):
                self.hit(user)
        self.assertEqual(len(self.limiter), 0)

    def test_a_negative_limit_is_treated_as_disabled(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", -1):
            self.assertTrue(all(d.allowed for d in self.burst(50)))

    def test_the_setting_is_read_at_call_time(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", 0):
            self.assertTrue(all(d.allowed for d in self.burst(10)))
        with mock.patch.object(config, "RATE_LIMIT_MAX", 2):
            self.assertEqual([d.allowed for d in self.burst(3, user=9)], [True, True, False])


class NoticeTests(LimiterTestCase):
    def test_the_first_rejection_asks_for_a_notice_and_the_rest_are_silent(self):
        self.burst(self.MAX)
        first, *rest = self.burst(20)
        self.assertEqual((first.allowed, first.notify), (False, True))
        self.assertEqual({(d.allowed, d.notify) for d in rest}, {(False, False)})

    def test_the_notice_carries_the_wait_and_so_do_the_silent_rejections(self):
        self.burst(self.MAX)
        self.clock.advance(2)
        first, second = self.hit(), self.hit()
        self.assertEqual((first.notify, first.retry_after), (True, 8))
        self.assertEqual((second.notify, second.retry_after), (False, 8))

    def test_at_most_one_notice_per_window(self):
        self.burst(self.MAX)
        self.clock.advance(1)
        self.assertTrue(self.hit().notify)                  # t=1: notice
        self.clock.advance(self.WINDOW - 0.5)               # t=10.5: the first three requests expired
        self.assertEqual([d.allowed for d in self.burst(self.MAX)], [True] * self.MAX)
        self.clock.advance(0.25)                            # t=10.75: blocked again, 9.75 s after the notice
        self.assertEqual((self.hit().allowed, self.hit().notify), (False, False))
        self.clock.advance(0.25)                            # t=11: a full window since the notice
        self.assertTrue(self.hit().notify)

    def test_notices_of_a_user_who_never_stops_are_at_least_a_window_apart(self):
        notices = []
        for _ in range(1000):
            decision = self.hit()
            if decision.notify:
                notices.append(self.clock.now)
            self.clock.advance(0.7)
        self.assertGreater(len(notices), 3)
        for earlier, later in zip(notices, notices[1:]):
            self.assertGreaterEqual(later - earlier, self.WINDOW)

    def test_allowed_requests_never_ask_for_a_notice(self):
        self.assertFalse(any(d.notify for d in self.burst(self.MAX)))


class CleanupTests(LimiterTestCase):
    def test_stale_entries_disappear_without_anyone_calling_sweep(self):
        for user in range(50):
            self.hit(user)
        self.assertEqual(len(self.limiter), 50)
        self.clock.advance(self.WINDOW + 1)
        self.hit(user=999)  # any later request triggers the sweep
        self.assertEqual(len(self.limiter), 1)

    def test_sweep_removes_only_users_with_nothing_left_to_remember(self):
        self.hit(user=1)
        self.clock.advance(8)
        self.hit(user=2)
        self.clock.advance(4)  # t=12: user 1 is 12 s old, user 2 only 4 s
        self.assertEqual(self.limiter.sweep(), 1)
        self.assertEqual(sorted(user for _, user in self.limiter._entries), [2])
        self.assertEqual(self.limiter.sweep(), 0)

    def test_a_swept_user_is_treated_like_a_new_one(self):
        self.burst(self.MAX)
        self.clock.advance(self.WINDOW)
        self.assertEqual(self.limiter.sweep(), 1)
        self.assertEqual([d.allowed for d in self.burst(self.MAX + 1)], [True] * self.MAX + [False])

    def test_an_entry_survives_while_its_notice_cooldown_is_running(self):
        with mock.patch.object(config, "RATE_LIMIT_MAX", 1):
            self.hit()                                  # t=0
            self.clock.advance(1)
            self.assertTrue(self.hit().notify)          # t=1: notice
            self.clock.advance(9.5)                     # t=10.5: no hits left, cooldown until t=11
            self.assertEqual(self.limiter.sweep(), 0)
            self.assertTrue(self.hit().allowed)         # t=10.5
            self.clock.advance(0.2)
            decision = self.hit()                       # t=10.7: blocked again, but the notice was 9.7 s ago
            self.assertEqual((decision.allowed, decision.notify), (False, False))
            self.clock.advance(self.WINDOW * 2)
            self.assertEqual(self.limiter.sweep(), 1)

    def test_memory_follows_recent_activity_not_the_number_of_users_ever_seen(self):
        biggest = 0
        for user in range(3000):  # a brand-new user every simulated second
            self.hit(user)
            self.clock.advance(1)
            biggest = max(biggest, len(self.limiter))
        self.assertLessEqual(biggest, 2 * self.WINDOW + 2)
        self.assertLess(len(self.limiter), 3000 / 100)

    def test_reset_forgets_everyone(self):
        self.burst(self.MAX + 1)
        self.limiter.reset()
        self.assertEqual(len(self.limiter), 0)
        self.assertEqual([d.allowed for d in self.burst(self.MAX)], [True] * self.MAX)


class ConcurrencyTests(LimiterTestCase):
    MAX = 5
    WINDOW = 60.0

    def setUp(self):
        super().setUp()
        # Switch threads as often as possible (and inside the critical section), so a missing lock is a
        # failure instead of a matter of luck.
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)
        patcher = mock.patch.object(ratelimit, "deque", YieldingDeque)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def run_threads(count, target):
        start = threading.Barrier(count)
        errors, results = [], [None] * count

        def worker(index):
            try:
                start.wait()
                results[index] = target(index)
            except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not any(t.is_alive() for t in threads), "a worker thread hung"
        return results, errors

    def test_every_public_method_waits_for_the_lock(self):
        calls = {
            "check": lambda: self.hit(user=1),
            "sweep": self.limiter.sweep,
            "reset": self.limiter.reset,
            "len": lambda: len(self.limiter),
        }
        threads = {name: threading.Thread(target=call) for name, call in calls.items()}
        with self.limiter._lock:  # someone else is in the middle of an update
            for thread in threads.values():
                thread.start()
            time.sleep(0.05)  # plenty for an unguarded method to finish; a guarded one cannot
            running = {name for name, thread in threads.items() if thread.is_alive()}
        for thread in threads.values():
            thread.join(timeout=20)
        self.assertEqual(running, set(calls))

    def test_threads_of_one_user_can_never_take_more_than_the_limit(self):
        threads, rounds, attempts = 4, 12, 4
        round_start = threading.Barrier(threads)

        def work(_):
            chunks = {}
            for user in range(rounds):
                round_start.wait()  # everyone attacks the same fresh bucket at the same moment
                chunks[user] = [self.hit(user=user) for _ in range(attempts)]
            return chunks

        results, errors = self.run_threads(threads, work)
        self.assertEqual(errors, [])
        for user in range(rounds):
            decisions = [d for chunk in results for d in chunk[user]]
            self.assertEqual(len(decisions), threads * attempts)
            self.assertEqual(sum(d.allowed for d in decisions), self.MAX, f"user {user}")
            self.assertEqual(sum(d.notify for d in decisions), 1, f"user {user}")  # one notice for the stampede
            self.assertEqual(len(self.entry(user).hits), self.MAX)

    def test_every_user_gets_exactly_their_own_budget(self):
        users = 12

        def work(index):
            platform = TG if index % 2 == 0 else BALE
            return [self.hit(user=index // 2, platform=platform).allowed for _ in range(8)]

        results, errors = self.run_threads(users, work)
        self.assertEqual(errors, [])
        self.assertEqual([sum(chunk) for chunk in results], [self.MAX] * users)

    def test_the_same_id_on_two_platforms_does_not_interfere_under_load(self):
        def work(index):
            return [self.hit(user=5, platform=TG if index % 2 == 0 else BALE).allowed for _ in range(8)]

        results, errors = self.run_threads(8, work)
        self.assertEqual(errors, [])
        tg_allowed = sum(sum(chunk) for i, chunk in enumerate(results) if i % 2 == 0)
        bale_allowed = sum(sum(chunk) for i, chunk in enumerate(results) if i % 2 == 1)
        self.assertEqual((tg_allowed, bale_allowed), (self.MAX, self.MAX))

    def test_sweeping_while_other_threads_add_users_is_safe(self):
        workers, users_each = 3, 12
        everyone_ready, workers_done = threading.Barrier(workers + 1), threading.Event()
        sweeper_errors = []

        def sweeper():
            try:
                everyone_ready.wait()
                while not workers_done.is_set():  # keep sweeping for as long as users are being added
                    self.clock.advance(0.5)
                    self.limiter.sweep()
            except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
                sweeper_errors.append(e)

        def add_users(index):
            everyone_ready.wait()
            return [self.hit(user=index * 1000 + n) for n in range(users_each)]

        with mock.patch.object(config, "RATE_LIMIT_WINDOW_SECONDS", 5.0):
            sweep_thread = threading.Thread(target=sweeper)
            sweep_thread.start()
            try:
                _, errors = self.run_threads(workers, add_users)
            finally:
                workers_done.set()
                sweep_thread.join(timeout=20)
            self.assertFalse(sweep_thread.is_alive())
            self.assertEqual(errors + sweeper_errors, [])
            self.assertLessEqual(len(self.limiter), workers * users_each)
            self.clock.advance(10)  # whatever is left is now a full window old
            self.limiter.sweep()
            self.assertEqual(len(self.limiter), 0)


class ToolMeteringTests(unittest.TestCase):
    def test_tools_that_call_a_provider_are_counted(self):
        for cmd in ("debug", "read", "arxiv_full"):
            with self.subTest(cmd=cmd):
                self.assertTrue(is_metered_tool(cmd))

    def test_wikipedia_and_arxiv_search_use_no_provider_quota(self):
        self.assertFalse(is_metered_tool("wiki"))
        self.assertFalse(is_metered_tool("arxiv"))

    def test_an_unknown_or_future_tool_is_counted_until_listed_as_free(self):
        self.assertTrue(is_metered_tool("some_new_tool"))

    def test_every_free_tool_is_a_real_tool(self):
        self.assertTrue(ratelimit.QUOTA_FREE_TOOLS <= set(TOOLS))


class ModuleLevelTests(unittest.TestCase):
    def test_check_rate_limit_uses_the_module_limiter_at_call_time(self):
        fake = RateLimiter(clock=FakeClock())
        with mock.patch.object(ratelimit, "limiter", fake), \
                mock.patch.object(config, "RATE_LIMIT_MAX", 1), \
                mock.patch.object(config, "RATE_LIMIT_WINDOW_SECONDS", 60.0):
            self.assertTrue(check_rate_limit(TG, 1).allowed)
            self.assertFalse(check_rate_limit(TG, 1).allowed)
        self.assertEqual(len(fake), 1)

    def test_the_default_limiter_works_with_the_real_clock(self):
        limiter = RateLimiter()
        with mock.patch.object(config, "RATE_LIMIT_MAX", 2), mock.patch.object(config, "RATE_LIMIT_WINDOW_SECONDS", 60.0):
            self.assertEqual([limiter.check(TG, 1).allowed for _ in range(3)], [True, True, False])


class ConfigTests(unittest.TestCase):
    def test_defaults_are_twenty_requests_per_sixty_seconds(self):
        with ReloadedConfig(RATE_LIMIT_MAX="", RATE_LIMIT_WINDOW_SECONDS="") as cfg:
            self.assertEqual((cfg.RATE_LIMIT_MAX, cfg.RATE_LIMIT_WINDOW_SECONDS), (20, 60.0))

    def test_environment_overrides(self):
        with ReloadedConfig(RATE_LIMIT_MAX="5", RATE_LIMIT_WINDOW_SECONDS="30") as cfg:
            self.assertEqual((cfg.RATE_LIMIT_MAX, cfg.RATE_LIMIT_WINDOW_SECONDS), (5, 30.0))
        with ReloadedConfig(RATE_LIMIT_WINDOW_SECONDS="2.5") as cfg:
            self.assertEqual(cfg.RATE_LIMIT_WINDOW_SECONDS, 2.5)

    def test_zero_is_kept_because_it_means_disabled(self):
        with ReloadedConfig(RATE_LIMIT_MAX="0") as cfg:
            self.assertEqual(cfg.RATE_LIMIT_MAX, 0)

    def test_malformed_values_fall_back_to_the_defaults(self):
        for limit in ("abc", "-3", "1.5", "nan", "  "):
            with self.subTest(limit=limit), ReloadedConfig(RATE_LIMIT_MAX=limit) as cfg:
                self.assertEqual(cfg.RATE_LIMIT_MAX, 20)
        for window in ("0", "0.5", "-1", "nan", "inf", "abc"):
            with self.subTest(window=window), ReloadedConfig(RATE_LIMIT_WINDOW_SECONDS=window) as cfg:
                self.assertEqual(cfg.RATE_LIMIT_WINDOW_SECONDS, 60.0)


if __name__ == "__main__":
    unittest.main()
