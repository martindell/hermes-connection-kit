"""Real SIGKILL/restart checks with private state and a durable fake GitHub.

No network, live registry, service changes or actual jobs. Not a Work crash test.
"""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import unittest

import recovery as r

w = r.w
RUN = 'run_' + 'c' * 32


class DurableGitHub:
    def __init__(self, path, crash=None):
        self.path, self.crash = path, crash

    def load(self):
        return json.loads(w._private_read(self.path))

    def comments(self):
        return self.load()['comments']

    def post(self, body):
        data = self.load()
        data['attempts'] += 1
        w._atomic(self.path, json.dumps(data).encode())
        if self.crash == 'before_accept':
            os.kill(os.getpid(), signal.SIGKILL)
        data['comments'].append(dict(id=101, body=body, user=dict(login='tester')))
        w._atomic(self.path, json.dumps(data).encode())
        if self.crash == 'after_accept':
            os.kill(os.getpid(), signal.SIGKILL)
        return 101


def tick(root, now, crash=None):
    root = Path(root)
    rec = r.Recovery(w.Store(root / 'observer'), r.RecoveryStore(root / 'recovery'),
                     'test', 'example/repo', 5, 'tester',
                     github_factory=lambda *args: DurableGitHub(root / 'github.json', crash),
                     clock=lambda: now)
    rec.tick()


@unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX fork and SIGKILL')
class RecoveryProcessCrashTest(unittest.TestCase):
    def run_child(self, root, now, crash=None):
        child = multiprocessing.get_context('fork').Process(target=tick, args=(str(root), now, crash))
        child.start()
        child.join(12)
        if child.is_alive():
            child.kill()
            child.join(3)
            self.fail('recovery child did not finish within 12 seconds')
        self.assertEqual(child.exitcode, -signal.SIGKILL if crash else 0)

    def scenario(self, crash):
        with tempfile.TemporaryDirectory(prefix='hermes-crash-test-', dir=Path.home()) as tmp:
            root = Path(tmp)
            source = w.Store(root / 'observer')
            store = r.RecoveryStore(root / 'recovery')
            watch = w.Watchdog(source, {'test': ('example/repo', 5)}, None, ack_actor='tester')
            watch.set_enabled(True)
            r.Recovery(source, store, 'test', 'example/repo', 5, 'tester').activate()
            state = source.load()
            state['runs'][RUN] = dict(project='test', repo='example/repo', pr=5,
                correlation='crash-test', deadline=5000, observed='completed',
                generations={}, blocked=False, overdue=False)
            event = watch._event(state, RUN, 'completion', 1000)
            event.update(delivery='delivered', comment_id=100, ack_deadline=87400)
            source.save(state)
            w._atomic(root / 'github.json', json.dumps(dict(attempts=0, comments=[
                dict(id=100, body=w.Watchdog._body(event), user=dict(login='tester'))])).encode())
            original = source.path.read_bytes()
            self.run_child(root, 1000)  # Enrol with the real grace interval.
            self.assertEqual(store.load()['records'][event['id']]['status'], 'waiting')
            self.run_child(root, 1000 + r.GRACE, crash)
            self.assertEqual(store.load()['records'][event['id']]['status'], 'attempted')
            # SIGKILL must release both locks without deleting lock/state files.
            with source.locked(), store.locked():
                pass
            for i in range(3):
                self.run_child(root, 1001 + r.GRACE + i)
            row = store.load()['records'][event['id']]
            remote = DurableGitHub(root / 'github.json').load()
            self.assertEqual(remote['attempts'], 1, 'restart must never replay POST')
            accepted = crash == 'after_accept'
            self.assertEqual(len(remote['comments']), 2 if accepted else 1)
            self.assertEqual(row['status'], 'delivered' if accepted else 'attempted')
            self.assertEqual(row['reminder_comment'], 101 if accepted else None)
            if accepted:
                self.assertEqual(remote['comments'][1]['body'], r.body(event))
                remote['comments'].append(dict(id=102, body=w.ack_marker(event['id'], 100),
                                               user=dict(login='tester')))
                w._atomic(root / 'github.json', json.dumps(remote).encode())
                self.run_child(root, 2000)
                self.assertEqual(store.load()['records'][event['id']]['status'], 'acknowledged')
            else:
                self.run_child(root, 1000 + r.LIFETIME)
                self.assertEqual(store.load()['records'][event['id']]['status'], 'expired')
            self.assertEqual(DurableGitHub(root / 'github.json').load()['attempts'], 1)
            self.assertEqual(source.path.read_bytes(), original, 'observer state changed')

    def test_sigkill_before_accept_sacrifices_delivery_without_replay(self):
        self.scenario('before_accept')

    def test_sigkill_after_accept_reconciles_one_reminder_and_ack(self):
        self.scenario('after_accept')


if __name__ == '__main__':
    unittest.main()
