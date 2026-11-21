import copy
import datetime
import json
from pathlib import Path
import tempfile
import unittest
import time
import signal
import subprocess
from unittest.mock import Mock

import baton
import baton_runner as r


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(repo="example/repo", issue=1, project="test", author="owner")
        self.state = dict(version=1, binding=baton.digest(self.config), session="a"*32,
                          hashes={}, core=baton.empty("test"), fault=False,
                          fault_generation=0, last_ok=0)

    def comment(self, cid=1, **fields):
        record = dict(id="event-1", project="test", task="task-1", seq=1,
                      actor="work", action="open", deadline=2000000000, source="source:1",
                      session="a"*32)
        record.update(fields)
        return dict(id=cid, user={"login":"owner"},
                    issue_url="https://api.github.com/repos/example/repo/issues/1",
                    created_at="2026-11-20T21:00:00Z", updated_at="2026-11-20T21:00:00Z",
                    body=r.MARKER+"\n"+json.dumps(record))

    def test_authored_record_and_server_timestamp(self):
        new = r.reconcile(self.state, [self.comment()], self.config)
        self.assertEqual(new["core"]["tasks"]["task-1"]["at"], 1795208400)

    def test_foreign_session_and_actor_not_adopted(self):
        foreign = self.comment(session="b"*32)
        other = self.comment(2)
        other["user"]["login"] = "outsider"
        new = r.reconcile(self.state, [foreign, other], self.config)
        self.assertEqual(new["core"]["tasks"], {})

    def test_missing_or_changed_accepted_comment_fails(self):
        original = self.comment()
        state = r.reconcile(self.state, [original], self.config)
        changed = self.comment(deadline=2000000001)
        for comments in ([], [changed]):
            with self.assertRaises(ValueError):
                r.reconcile(state, comments, self.config)

    def test_duplicate_or_wrong_route_rejected(self):
        wrong = self.comment()
        wrong["issue_url"] += "2"
        for comments in ([self.comment(), self.comment()], [wrong]):
            with self.assertRaises(ValueError):
                r.reconcile(self.state, comments, self.config)

    def test_edits_and_forged_timestamp_rejected(self):
        edited = self.comment()
        edited["updated_at"] = "2026-11-20T21:01:00Z"
        for comment in (edited, self.comment(at=123)):
            with self.assertRaises(ValueError):
                r.reconcile(self.state, [comment], self.config)

    def test_json_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            r.decode('{"a":1,"a":2}')

    def test_read_failure_keeps_last_state_and_reports_unknown(self):
        state = r.reconcile(self.state, [self.comment()], self.config)
        def failed():
            raise OSError("private transport detail")
        new, incidents, fault = r.check(state, self.config, failed, 2100000000)
        self.assertTrue(fault)
        self.assertEqual(new["core"], state["core"])
        self.assertEqual(incidents[0]["kind"], "monitoring-unavailable")
        again, repeated, _ = r.check(new, self.config, failed, 2100000060)
        self.assertEqual(incidents, repeated)
        recovered, _, fault = r.check(again, self.config, lambda:[self.comment()], 2100000120)
        self.assertFalse(fault)
        _, next_incidents, _ = r.check(recovered, self.config, failed, 2100000180)
        self.assertNotEqual(next_incidents[0]["id"], incidents[0]["id"])

    def test_invalid_later_record_does_not_partially_advance(self):
        bad = self.comment(2, id="event-2", seq=9)
        state, _, fault = r.check(self.state, self.config,
                                 lambda:[self.comment(), bad], 100)
        self.assertTrue(fault)
        self.assertEqual(state["core"], self.state["core"])

    def test_atomic_state_permissions_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"state.json"
            r.atomic(path, self.state)
            self.assertEqual(r.decode(r.private_read(path)), self.state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            link = Path(temp)/"alias"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                r.private_read(link)

    def test_paginated_read_failure_discards_partial_snapshot(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, limit): return json.dumps([{}]*100).encode()
        class Opener:
            count = 0
            def open(self, request, timeout):
                self.count += 1
                if self.count == 2:
                    raise OSError("failed second page")
                return Response()
        with self.assertRaises(OSError):
            r.GitHub("example/repo", 1, "test-token", opener=Opener()).comments()

    def test_blocked_read_is_interrupted_and_reports_monitor_fault(self):
        before = signal.getsignal(signal.SIGALRM)
        def blocked():
            with r.read_deadline(0.02):
                time.sleep(1)
        start = time.monotonic()
        new, incidents, failed = r.check(self.state, self.config, blocked, 100)
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertTrue(failed)
        self.assertEqual(incidents[0]["kind"], "monitoring-unavailable")
        self.assertEqual(new["core"], self.state["core"])
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_truncated_hash_ledger_cannot_drop_saved_obligation(self):
        state = r.reconcile(self.state, [self.comment()], self.config)
        state["hashes"] = {}
        new, incidents, failed = r.check(state, self.config, lambda:[], 100)
        self.assertTrue(failed)
        self.assertEqual(new["core"], state["core"])
        self.assertEqual(incidents[0]["kind"], "monitoring-unavailable")

    def test_corrupt_core_cannot_be_silently_replaced(self):
        state = r.reconcile(self.state, [self.comment()], self.config)
        state["core"]["tasks"]["task-1"]["deadline"] += 1
        with self.assertRaises(ValueError):
            r.reconcile(state, [self.comment()], self.config)

    def test_null_author_is_a_bounded_journal_fault(self):
        comment = self.comment()
        comment["user"] = None
        _, incidents, failed = r.check(self.state, self.config, lambda:[comment], 100)
        self.assertTrue(failed)
        self.assertEqual(incidents[0]["kind"], "monitoring-unavailable")

    def test_local_fault_retry_is_durable_bounded_and_spaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = Mock(side_effect=subprocess.TimeoutExpired("notify", 15))
            for now in (1000, 1001, 1900, 1901, 2800, 4000):
                r.local_fault(root, run=run, now=now)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(len(list(root.glob("monitor-fault.*.intent"))), 3)

    def test_local_fault_success_stops_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = Mock()
            r.local_fault(root, run=run, now=1000)
            r.local_fault(root, run=run, now=3000)
            run.assert_called_once()
            self.assertTrue((root / "monitor-fault.submitted").exists())


if __name__ == "__main__":
    unittest.main()
