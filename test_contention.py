"""Real flock contention on private temporary state; no network or model calls."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
import unittest

import recovery as r
import watchdog as w
from test_recovery import FakeGitHub, ROUTE, RUN


class ContentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.source = w.Store(Path(self.temp.name) / 'observer')
        self.store = r.RecoveryStore(Path(self.temp.name) / 'recovery')
        self.now = 1000
        self.gh = FakeGitHub()
        self.calls = []
        owner = self
        class Gateway:
            def status(self, run):
                owner.calls.append(run)
                return {'run_id': run, 'status': 'completed'}
        self.watch = w.Watchdog(self.source, {'test': ROUTE}, Gateway(),
                               github_factory=lambda *a: self.gh,
                               clock=lambda: self.now, ack_actor='tester')
        self.watch.set_enabled(True)
        self.rec = r.Recovery(self.source, self.store, 'test', *ROUTE, 'tester',
                              github_factory=lambda *a: self.gh, clock=lambda: self.now)
        self.rec.activate()
        self.watch.register('test', *ROUTE, RUN, 'test-1', 5000)

    def test_waiting_tick_progresses_after_real_lock_release(self):
        for tick in (self.watch.bounded_tick, self.rec.tick):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.source.locked():
                    f = pool.submit(tick)
                    time.sleep(.15)
                    self.assertFalse(f.done(), 'tick must wait rather than fail immediately')
                self.assertIsInstance(f.result(timeout=3), dict)
        self.assertEqual(self.source.load()['runs'][RUN]['observed'], 'completed')
        self.assertEqual(len(self.store.load()['records']), 1)

    def test_sustained_contention_is_bounded_and_has_no_side_effects(self):
        before = (self.source.path.read_bytes(), self.store.path.read_bytes())
        with patch.object(w, 'TICK_LOCK_WAIT', .15), patch.object(r.w, 'TICK_LOCK_WAIT', .15):
            with self.source.locked():
                for tick in (self.watch.bounded_tick, self.rec.tick):
                    started = time.monotonic()
                    with self.assertRaises(BlockingIOError):
                        tick()
                    self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(before, (self.source.path.read_bytes(), self.store.path.read_bytes()))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gh.posts, [])

    def test_invalid_waits_rejected_and_control_stays_fail_fast(self):
        for value in (-1, 6, float('nan'), float('inf'), True, '1'):
            with self.assertRaises(ValueError):
                with self.source.locked(wait_seconds=value):
                    self.fail('invalid wait acquired')
        with self.source.locked():
            started = time.monotonic()
            with self.assertRaises(BlockingIOError):
                self.watch.tracked(RUN)
            self.assertLess(time.monotonic() - started, .5)

    def test_lock_wait_counts_against_watchdog_admission_budget(self):
        elapsed = [0]
        real_lock = self.source.locked
        @contextlib.contextmanager
        def delayed_lock(**kwargs):
            with real_lock(**kwargs):
                elapsed[0] += w.TICK_BUDGET
                yield
        self.watch.monotonic = lambda: elapsed[0]
        with patch.object(self.source, 'locked', delayed_lock):
            self.watch.bounded_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gh.posts, [])

    def test_both_timer_orders_progress_without_duplicate_reminder(self):
        # Ensure the opposing tick actually starts while the winner holds flock.
        for winner in ('watchdog', 'recovery'):
            entered, release = threading.Event(), threading.Event()
            real_lock = self.source.locked
            winner_thread = [None]
            @contextlib.contextmanager
            def held_lock(**kwargs):
                with real_lock(**kwargs):
                    if threading.get_ident() == winner_thread[0]:
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('test did not release winner')
                    yield
            def first():
                winner_thread[0] = threading.get_ident()
                return (self.watch.bounded_tick if winner == 'watchdog' else self.rec.tick)()
            second = self.rec.tick if winner == 'watchdog' else self.watch.bounded_tick
            with patch.object(self.source, 'locked', held_lock), ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(first)
                try:
                    self.assertTrue(entered.wait(2))
                    b = pool.submit(second)
                    time.sleep(.15)
                    self.assertFalse(b.done())
                finally:
                    release.set()
                a.result(timeout=3)
                b.result(timeout=3)
        self.rec.tick()
        self.now += r.GRACE
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(self.watch.bounded_tick), pool.submit(self.rec.tick)
            a.result(timeout=3)
            b.result(timeout=3)
        for _ in range(3):
            self.rec.tick()
            self.watch.bounded_tick()
        reminders = [p for p in self.gh.posts if p.startswith('<!-- hermes-watchdog:recovery:')]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(self.source.load()['runs'][RUN]['observed'], 'completed')
        self.assertEqual(len(self.source.load()['events']), 1)

    def test_companion_lock_is_not_waited_on_while_source_held(self):
        with self.store.locked():
            started = time.monotonic()
            with self.assertRaises(BlockingIOError):
                self.rec.tick()
            self.assertLess(time.monotonic() - started, .5)
            with self.source.locked():
                pass  # Failed companion acquisition released source lock.
        self.assertEqual(self.gh.posts, [])

if __name__ == '__main__':
    unittest.main()
