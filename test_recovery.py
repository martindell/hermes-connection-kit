"""Offline invented events and fake GitHub; no live jobs or network."""
import json
from pathlib import Path
import tempfile
import unittest

import recovery as r
import watchdog as w

RUN = 'run_' + 'a' * 32
ROUTE = ('example/repo', 5)


class FakeGitHub:
    def __init__(self):
        self.items, self.posts = [], []
        self.fail_get = self.ambiguous = self.fail_before = False
        self.before_post = lambda: None

    def comments(self):
        if self.fail_get:
            raise TimeoutError('fictional failure')
        return list(self.items)

    def add(self, body, actor='tester'):
        cid = 100 + len(self.items)
        self.items.append(dict(id=cid, body=body, user=dict(login=actor)))
        return cid

    def post(self, body):
        self.before_post()
        self.posts.append(body)
        if self.fail_before:
            raise TimeoutError('before acceptance')
        cid = self.add(body)
        if self.ambiguous:
            raise TimeoutError('lost response')
        return cid


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.source = w.Store(Path(self.temp.name) / 'observer')
        self.store = r.RecoveryStore(Path(self.temp.name) / 'recovery')
        self.now = 1000
        self.gh = FakeGitHub()
        self.watch = w.Watchdog(self.source, {'test': ROUTE}, None, ack_actor='tester')
        self.watch.set_enabled(True)
        self.new_recovery().activate()

    def new_recovery(self, **kwargs):
        return r.Recovery(self.source, self.store, 'test', *ROUTE, 'tester',
                          github_factory=lambda *args: self.gh, clock=lambda: self.now, **kwargs)

    def event(self):
        s = self.source.load()
        s['runs'][RUN] = dict(project='test', repo=ROUTE[0], pr=5, correlation='test-1',
            deadline=5000, observed='completed', generations={}, blocked=False, overdue=False)
        e = self.watch._event(s, RUN, 'completion', self.now)
        e.update(delivery='delivered', ack_deadline=self.now+86400)
        e['comment_id'] = self.gh.add(w.Watchdog._body(e))
        self.source.save(s)
        self.e = e
        self.new_recovery().tick()
        self.now += r.GRACE
        return e

    def row(self):
        return self.store.load()['records'][self.e['id']]

    def ack(self):
        self.gh.add(w.ack_marker(self.e['id'], self.e['comment_id']))

    def test_callback_exit_wakes_again_once_and_original_ack_finishes(self):
        self.event()
        before = self.source.path.read_bytes()
        self.new_recovery().tick()
        self.assertEqual(len(self.gh.posts), 1)
        self.assertTrue(self.gh.posts[0].startswith('<!-- hermes-watchdog:recovery:'))
        self.assertIn('Original comment: 100', self.gh.posts[0])
        self.assertNotIn('hermes-watchdog:event:', self.gh.posts[0])
        for _ in range(3):
            self.now += 1000
            self.new_recovery().tick()
        self.assertEqual(len(self.gh.posts), 1)
        self.ack()
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'acknowledged')
        self.assertEqual(before, self.source.path.read_bytes())

    def test_original_ack_before_reminder_suppresses_write(self):
        self.event()
        self.ack()
        self.new_recovery().tick()
        self.assertEqual(self.gh.posts, [])
        self.assertEqual(self.row()['status'], 'acknowledged')

    def test_durable_observer_ack_suppresses_reminder_without_github(self):
        self.event()
        s = self.source.load()
        s['events'][self.e['id']]['ack'] = 'acknowledged'
        self.source.save(s)
        self.gh.comments = lambda: self.fail('durable ack must avoid network')
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'acknowledged')
        self.assertEqual(self.gh.posts, [])

    def test_ack_races_post_next_tick_reconciles_without_second_post(self):
        self.event()
        self.gh.before_post = self.ack
        self.new_recovery().tick()
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'acknowledged')
        self.assertEqual(len(self.gh.posts), 1)

    def test_ambiguous_post_reconciles_after_process_restart(self):
        self.event()
        self.gh.ambiguous = True
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'attempted')
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'delivered')
        self.assertEqual(len(self.gh.posts), 1)

    def test_unaccepted_ambiguous_post_never_replayed_and_expires(self):
        self.event()
        self.gh.fail_before = True
        self.new_recovery().tick()
        self.now += 1000
        self.new_recovery().tick()
        self.assertEqual(len(self.gh.posts), 1)
        self.now += r.LIFETIME
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'expired')

    def test_failed_read_never_allows_write(self):
        self.event()
        self.gh.fail_get = True
        self.new_recovery().tick()
        self.assertEqual(self.gh.posts, [])
        self.gh.fail_get = False
        self.new_recovery().tick()
        self.assertEqual(len(self.gh.posts), 1)

    def test_paused_observer_or_companion_performs_no_network(self):
        self.event()
        self.watch.set_enabled(False)
        self.gh.comments = lambda: self.fail('unexpected network read')
        self.new_recovery().tick()
        self.watch.set_enabled(True)
        self.new_recovery().set_enabled(False)
        self.new_recovery().tick()

    def test_old_runs_excluded_and_activation_cannot_reset_history(self):
        self.event()
        other = r.RecoveryStore(Path(self.temp.name) / 'other')
        rec = r.Recovery(self.source, other, 'test', *ROUTE, 'tester',
                         github_factory=lambda *args: self.gh, clock=lambda: self.now)
        rec.activate()
        rec.tick()
        self.assertEqual(other.load()['records'], {})
        with self.assertRaises(ValueError):
            rec.activate()

    def test_route_drift_fails_closed(self):
        self.event()
        s = self.source.load()
        s['events'][self.e['id']]['pr'] = 6
        self.source.save(s)
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'conflict')
        self.assertEqual(self.gh.posts, [])

    def test_wrong_actor_or_edited_original_cannot_trigger_recovery(self):
        self.event()
        self.gh.items[0]['user']['login'] = 'stranger'
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'conflict')
        self.assertEqual(self.gh.posts, [])

    def test_duplicate_reminder_requires_manual_review(self):
        self.event()
        self.gh.add(r.body(self.e))
        self.gh.add(r.body(self.e))
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'conflict')
        self.assertEqual(self.gh.posts, [])

    def test_grace_and_expiry(self):
        self.event()
        self.now -= 1
        self.new_recovery().tick()
        self.assertEqual(self.gh.posts, [])
        self.now += r.LIFETIME
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'expired')
        self.assertEqual(self.gh.posts, [])

    def test_delivered_reminder_expires_without_second_post(self):
        self.event()
        self.new_recovery().tick()
        self.now += r.LIFETIME
        self.new_recovery().tick()
        self.assertEqual(self.row()['status'], 'expired')
        self.assertEqual(len(self.gh.posts), 1)

    def test_source_lock_contention_prevents_all_actions(self):
        self.event()
        with self.source.locked():
            with self.assertRaises(BlockingIOError):
                self.new_recovery().tick()
        self.assertEqual(self.gh.posts, [])

    def test_process_death_after_intent_does_not_repost(self):
        self.event()
        s = self.store.load()
        s['records'][self.e['id']]['status'] = 'attempted'
        self.store.save(s)
        self.new_recovery().tick()
        self.assertEqual(self.gh.posts, [])

    def test_bad_hash_is_not_enrolled(self):
        self.event()
        s = self.source.load()
        e = dict(self.e)
        e['id'] = 'f' * 64
        s['events'][e['id']] = e
        self.source.save(s)
        self.new_recovery().tick()
        self.assertNotIn(e['id'], self.store.load()['records'])
        self.assertEqual(len(self.gh.posts), 1)  # Only the valid original.

    def test_one_post_per_tick_and_other_event_survives_restart(self):
        self.event()
        s = self.source.load()
        e = self.watch._event(s, RUN, 'overdue', self.now)
        e.update(delivery='delivered', ack_deadline=self.now+86400)
        e['comment_id'] = self.gh.add(w.Watchdog._body(e))
        self.source.save(s)
        self.new_recovery().tick()
        self.assertEqual(len(self.gh.posts), 1)
        self.now += r.GRACE
        self.new_recovery().tick()
        self.assertEqual(len(self.gh.posts), 2)

    def test_binding_change_and_corrupt_state_rejected(self):
        s = self.store.load()
        s['binding']['actor'] = 'stranger'
        self.store.save(s)
        with self.assertRaises(ValueError):
            self.new_recovery().tick()
        self.store.path.write_text('{"version":1,"enabled":true,"excluded_runs":[],"records":{"bad":{}},"binding":{}}')
        with self.assertRaises(ValueError):
            self.store.load()


if __name__ == '__main__':
    unittest.main()
