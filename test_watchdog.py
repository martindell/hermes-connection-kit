"""Offline invented runs only: never calls a real gateway, GitHub or model."""
import importlib.util
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import multiprocessing
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).with_name("watchdog.py"))
assert spec is not None and spec.loader is not None
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
RUN = "run_" + "a" * 32
RUN2 = "run_" + "b" * 32
ROUTE = ("example/private-repository", 42)


class Clock:
    now = 1000
    def __call__(self):
        return self.now


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.responses: dict[str, str | dict | Exception] = {RUN: "running", RUN2: "running"}
        self.fail = False
    def status(self, run):
        self.calls.append(run)
        if self.fail:
            raise OSError("fictional transport outage")
        value = self.responses[run]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict):
            return value
        return {"run_id": run, "status": value, "updated_at": 1000, "last_event": "run.running"}


class FakeGitHub:
    comments_store = []
    posts = []
    fail_after_post = False
    fail_get = False
    def __init__(self, repo, pr, actor):
        assert (repo, pr) == ROUTE, "wrong route"
        assert actor == "example-actor"
    def comments(self):
        if self.fail_get:
            raise OSError("offline")
        return list(self.comments_store)
    def post(self, body):
        self.posts.append(body)
        cid = 100 + len(self.posts)
        self.comments_store.append({"id": cid, "body": body, "user": {"login": "example-actor"}})
        if self.fail_after_post:
            raise TimeoutError("ambiguous POST")
        return cid


def competing_lock(directory, queue):
    try:
        with w.Store(directory).locked():
            queue.put("acquired")
    except BlockingIOError:
        queue.put("blocked")


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = w.Store(Path(self.temp.name) / "private")
        self.clock = Clock()
        self.gateway = FakeGateway()
        FakeGitHub.comments_store = []
        FakeGitHub.posts = []
        FakeGitHub.fail_get = False
        FakeGitHub.fail_after_post = False
        self.watch = w.Watchdog(self.store, {"example": ROUTE}, self.gateway, FakeGitHub, self.clock)
        self.watch.register("example", *ROUTE, RUN, "source-10", 4600)
        self.watch.set_enabled(True)

    def events(self, state, kind):
        return [e for e in state["events"].values() if e["kind"] == kind]

    def test_21_fast_active_jobs_checked_and_multiple_events_delivered_in_one_tick(self):
        ids = ['run_' + f'{i:032x}' for i in range(21)]
        for run_id in ids:
            self.watch.register('example', *ROUTE, run_id, 'source-11', 4600)
            self.gateway.responses[run_id] = 'completed'
        self.gateway.responses[RUN] = 'running'
        state = self.watch.bounded_tick()
        self.assertEqual(set(self.gateway.calls), set(ids + [RUN]))
        self.assertEqual(len(self.events(state, 'completion')), 21)
        self.assertEqual(len(FakeGitHub.posts), 21)
        self.assertTrue(all(e['delivery'] == 'delivered' for e in self.events(state, 'completion')))
        self.clock.now += 60
        resumed = w.Watchdog(w.Store(self.store.directory), {'example': ROUTE},
                             self.gateway, FakeGitHub, self.clock)
        resumed.bounded_tick()
        self.assertEqual(len(FakeGitHub.posts), 21)
        self.assertEqual(self.gateway.calls.count(RUN), 2)

    def test_slow_calls_budget_fair_delivery_ack_pause_and_restart(self):
        elapsed = [0]
        class SlowGateway(FakeGateway):
            def status(inner, run):
                elapsed[0] += 9
                return super().status(run)
        class SlowGitHub(FakeGitHub):
            def comments(inner):
                elapsed[0] += 8
                return super().comments()
            def post(inner, body):
                elapsed[0] += 10
                return super().post(body)
        gateway = SlowGateway()
        gateway.responses[RUN] = 'completed'
        ids = ['run_' + f'{i:032x}' for i in range(20)]
        for run_id in ids:
            self.watch.register('example', *ROUTE, run_id, 'source-11', 4600)
            gateway.responses[run_id] = 'running'
        self.watch.gateway = gateway
        self.watch.github_factory = SlowGitHub
        self.watch.monotonic = lambda: elapsed[0]
        minute = -1
        for minute in range(12):
            before = elapsed[0]
            self.watch.bounded_tick()
            self.assertLess(elapsed[0] - before, 45)
            self.clock.now += 60
            if len(gateway.calls) >= 21 and FakeGitHub.posts:
                break
        self.assertLess(minute, 11)
        self.assertEqual(len(FakeGitHub.posts), 1)
        event, = self.events(self.store.load(), 'completion')
        self.assertEqual(event['delivery'], 'delivered')
        self.assertNotEqual(event['ack'], 'acknowledged')
        self.watch.set_enabled(False)
        prior = (elapsed[0], len(gateway.calls), len(FakeGitHub.posts))
        self.watch.bounded_tick()
        self.assertEqual((elapsed[0], len(gateway.calls), len(FakeGitHub.posts)), prior)
        self.watch.set_enabled(True)
        FakeGitHub.comments_store.append({'id': 900, 'body': w.ack_marker(event['id'], event['comment_id']),
                                          'user': {'login': 'example-actor'}})
        resumed = w.Watchdog(w.Store(self.store.directory), {'example': ROUTE},
                             gateway, SlowGitHub, self.clock, monotonic=lambda: elapsed[0])
        resumed.bounded_tick()
        self.assertEqual(len(FakeGitHub.posts), 1)
        self.assertEqual(self.events(self.store.load(), 'completion')[0]['ack'], 'acknowledged')

    def test_32_slow_runs_with_busy_outbox_finish_cycle_without_starving_delivery(self):
        elapsed = [0]
        class SlowGateway(FakeGateway):
            def status(inner, run):
                elapsed[0] += 9
                return super().status(run)
        class BusyGitHub(FakeGitHub):
            def comments(inner):
                elapsed[0] += 8
                return super().comments()
            def post(inner, body):
                elapsed[0] += 18
                return super().post(body)
        gateway = SlowGateway()
        self.watch.gateway = gateway
        self.watch.github_factory = BusyGitHub
        self.watch.monotonic = lambda: elapsed[0]
        ids = [RUN] + ['run_' + f'{i:032x}' for i in range(31)]
        for run_id in ids[1:]:
            self.watch.register('example', *ROUTE, run_id, 'source-11', 4600)
        gateway.responses = {run_id: 'running' for run_id in ids}
        with self.store.locked():
            state = self.store.load()
            for run_id in ids:
                self.watch._event(state, run_id, 'connection-blocked', self.clock.now)
            self.store.save(state)
        minute = -1
        for minute in range(36):
            before = elapsed[0]
            self.watch.bounded_tick()
            self.assertLess(elapsed[0] - before, 45)
            self.clock.now += 60
            if set(gateway.calls) == set(ids):
                break
        self.assertLess(minute, 35, (len(set(gateway.calls)), set(ids) - set(gateway.calls), len(FakeGitHub.posts)))
        self.assertEqual(len(set(gateway.calls)), 32)
        self.assertGreater(len(FakeGitHub.posts), 0)
        self.assertEqual(len(FakeGitHub.posts), len(FakeGitHub.comments_store))

    def test_bounded_ambiguous_post_pause_restart_reconciles_without_replay(self):
        self.gateway.responses[RUN] = 'completed'
        FakeGitHub.fail_after_post = True
        state = self.watch.bounded_tick()
        event, = self.events(state, 'completion')
        self.assertEqual(event['delivery'], 'pending')
        self.assertEqual(len(FakeGitHub.posts), 1)
        self.watch.set_enabled(False)
        self.clock.now += 121
        self.assertEqual(self.watch.bounded_tick()['events'][event['id']]['delivery'], 'pending')
        FakeGitHub.fail_after_post = False
        resumed = w.Watchdog(w.Store(self.store.directory), {'example': ROUTE},
                             self.gateway, FakeGitHub, self.clock)
        resumed.set_enabled(True)
        resumed.bounded_tick()
        self.assertEqual(self.store.load()['events'][event['id']]['delivery'], 'delivered')
        self.assertEqual(len(FakeGitHub.posts), 1)
        self.assertEqual(self.gateway.calls, [RUN])

    def test_persisted_key_cursor_adapts_to_removed_members_and_legacy_integer(self):
        self.assertEqual(w._next_key(['a', 'b', 'c'], 'b'), 'c')
        self.assertEqual(w._next_key(['a', 'c'], 'b'), 'c')
        self.assertEqual(w._next_key(['a', 'c'], 'c'), 'a')
        self.assertEqual(w._next_key(['a', 'c'], 1), 'c')
        with self.assertRaises(ValueError):
            w._next_key(['a'], True)

    def test_active_capacity_fails_closed_without_mutation_or_replay(self):
        for i in range(w.MAX_ACTIVE_RUNS - 1):
            self.watch.register('example', *ROUTE, 'run_' + f'{i:032x}', 'source-11', 4600)
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'capacity'):
            self.watch.register('example', *ROUTE, RUN2, 'source-11', 4600)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(FakeGitHub.posts, [])

    def test_managed_tunnel_crash_with_healthy_gateway_manual_pause_and_recovery(self):
        class Tunnel:
            up = False
            def healthy(self):
                return self.up
        tunnel = Tunnel()
        self.watch.tunnel = tunnel
        self.watch.bounded_tick()
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(len(self.events(self.store.load(), 'connection-blocked')), 1)
        self.watch.set_enabled(False)
        for _ in range(3):
            self.watch.bounded_tick()
        self.assertEqual(len(self.events(self.store.load(), 'connection-blocked')), 1)
        tunnel.up = True
        self.watch.set_enabled(True)
        for _ in range(3):
            self.watch.bounded_tick()
        self.assertGreaterEqual(self.gateway.calls.count(RUN), 1)
        self.assertFalse(self.store.load()['runs'][RUN]['blocked'])
        tunnel.up = False
        for _ in range(3):
            self.watch.bounded_tick()
        self.assertEqual(len(self.events(self.store.load(), 'connection-blocked')), 2)

    def test_managed_tunnel_reads_only_exact_unit_and_fragment(self):
        tunnel = w.ManagedTunnel('example.service', '/home/example/example.service')
        with patch.object(w.subprocess, 'run', return_value=type('Result', (), {
                'stdout': 'ActiveState=active\nFragmentPath=/home/example/example.service\n'})()) as call:
            self.assertTrue(tunnel.healthy())
            self.assertEqual(call.call_args.args[0][:3], ['/usr/bin/systemctl', '--user', 'show'])
        with patch.object(w.subprocess, 'run', return_value=type('Result', (), {
                'stdout': 'ActiveState=active\nFragmentPath=/other.service\n'})()):
            self.assertFalse(tunnel.healthy())

    def test_completion_once_and_durable_result(self):
        self.gateway.responses[RUN] = {"run_id": RUN, "status": "completed", "output": "private text"}
        state = self.watch.tick()
        e, = self.events(state, "completion")
        self.assertEqual(e["delivery"], "delivered")
        self.assertIn(RUN, FakeGitHub.posts[0])
        self.assertNotIn("private text", FakeGitHub.posts[0])
        self.assertIn("private text", (self.store.directory / ("result-" + e["id"] + ".json")).read_text())
        self.assertEqual(len(FakeGitHub.posts), 1)
        self.watch.tick()
        self.assertEqual(len(FakeGitHub.posts), 1)
        self.assertEqual(self.gateway.calls, [RUN])  # terminal run never polled again
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_restart_reconciliation_no_repeat(self):
        self.gateway.responses[RUN] = "completed"
        first = self.watch.tick()
        other = w.Watchdog(w.Store(self.store.directory), {"example": ROUTE}, self.gateway, FakeGitHub, self.clock)
        second = other.tick()
        self.assertEqual(first["events"], second["events"])
        self.assertEqual(len(FakeGitHub.posts), 1)

    def test_ambiguous_post_reconciled_by_exact_marker(self):
        self.gateway.responses[RUN] = "completed"
        FakeGitHub.fail_after_post = True
        event, = self.events(self.watch.tick(), "completion")
        self.assertEqual(event["delivery"], "pending")
        FakeGitHub.fail_after_post = False
        self.clock.now += 121
        event, = self.events(self.watch.tick(), "completion")
        self.assertEqual(event["delivery"], "delivered")
        self.assertEqual(len(FakeGitHub.posts), 1)

    def test_running_http200_and_updated_at_not_progress(self):
        self.watch.tick()
        self.clock.now = 4599
        self.assertFalse(self.events(self.watch.tick(), "overdue"))
        self.clock.now = 4600
        self.assertEqual(len(self.events(self.watch.tick(), "overdue")), 1)
        self.clock.now += 10000
        self.assertEqual(len(self.events(self.watch.tick(), "overdue")), 1)

    def test_waiting_transition_once_then_new_generation(self):
        self.gateway.responses[RUN] = "waiting_for_approval"
        self.watch.tick()
        self.clock.now = 8000
        state = self.watch.tick()
        self.assertFalse(self.events(state, "overdue"))
        self.gateway.responses[RUN] = "running"
        self.watch.tick()
        self.gateway.responses[RUN] = "waiting_for_approval"
        state = self.watch.tick()
        self.assertEqual([e["generation"] for e in self.events(state, "waiting-for-approval")], [1, 2])

    def test_pause_suppresses_gateway_and_delivery_then_resumes(self):
        self.watch.set_enabled(False)
        self.gateway.responses[RUN] = "completed"
        self.assertFalse(self.watch.tick()["events"])
        self.assertEqual(self.gateway.calls, [])
        self.watch.set_enabled(True)
        self.assertEqual(len(self.events(self.watch.tick(), "completion")), 1)

    def test_outage_once_and_recovery(self):
        self.gateway.fail = True
        self.watch.tick()
        self.watch.tick()
        self.assertEqual(len(self.events(self.store.load(), "connection-blocked")), 1)
        self.gateway.fail = False
        self.watch.tick()
        self.gateway.fail = True
        self.assertEqual(len(self.events(self.watch.tick(), "connection-blocked")), 2)

    def test_incomplete_read_is_one_durable_block_and_recovers(self):
        self.gateway.responses[RUN] = IncompleteRead(b"private partial body", 20)
        for _ in range(2):
            state = self.watch.tick()
        self.assertTrue(self.store.load()["runs"][RUN]["blocked"])
        self.assertEqual(len(self.events(state, "connection-blocked")), 1)
        self.assertNotIn("private partial body", self.store.path.read_text())
        self.gateway.responses[RUN] = "completed"
        state = self.watch.tick()
        self.assertFalse(self.store.load()["runs"][RUN]["blocked"])
        self.assertEqual(len(self.events(state, "completion")), 1)
        self.assertEqual(len(self.events(state, "connection-blocked")), 1)

    def test_programming_error_is_not_hidden_as_outage(self):
        self.gateway.responses[RUN] = TypeError("fictional programming fault")
        with self.assertRaises(TypeError):
            self.watch.tick()
        self.assertFalse(self.store.load()["events"])

    def test_truncated_local_http_adapter_recovers(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(json.dumps({"run_id": RUN, "status": "completed"}).encode())
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        credential = self.store.directory / "credential"
        credential.write_bytes(b"fictional-token")
        credential.chmod(0o600)
        self.gateway = w.Gateway(f"http://127.0.0.1:{server.server_port}", credential)
        self.watch.gateway = self.gateway
        try:
            with self.assertRaises(IncompleteRead):
                self.gateway.status(RUN)
            state = self.watch.tick()
            self.assertEqual(len(self.events(state, "connection-blocked")), 1)
            self.assertEqual(len(self.events(self.watch.tick(), "connection-blocked")), 1)
        finally:
            server.shutdown()
            thread.join(2)
        # The complete JSON prefix must not be mistaken for a completed run.
        self.watch.gateway = FakeGateway()
        self.watch.gateway.responses[RUN] = "completed"
        state = self.watch.tick()
        self.assertFalse(state["runs"][RUN]["blocked"])
        self.assertEqual(len(self.events(state, "completion")), 1)

    def test_transport_failure_no_post_and_later_retry(self):
        FakeGitHub.fail_get = True
        self.gateway.responses[RUN] = "failed"
        e, = self.events(self.watch.tick(), "failed")
        self.assertEqual(e["delivery"], "pending")
        self.assertFalse(FakeGitHub.posts)
        FakeGitHub.fail_get = False
        self.clock.now += 121
        self.assertEqual(self.events(self.watch.tick(), "failed")[0]["delivery"], "delivered")

    def test_get_failures_count_towards_bounded_attempts(self):
        self.gateway.responses[RUN] = "completed"
        FakeGitHub.fail_get = True
        for _ in range(6):
            self.watch.tick()
            self.clock.now += 4000
        e, = self.events(self.store.load(), "completion")
        self.assertEqual((e["attempts"], e["delivery"]), (5, "exhausted"))
        self.assertFalse(FakeGitHub.posts)

    def test_route_drift_does_not_query_or_deliver(self):
        other = w.Watchdog(self.store, {"example": ("example/other-repository", 17)},
                           self.gateway, FakeGitHub, self.clock)
        self.assertFalse(other.tick()["events"])
        self.assertEqual(self.gateway.calls, [])

    def test_overdue_then_late_completion_preserves_both(self):
        self.clock.now = 4600
        self.watch.tick()
        self.gateway.responses[RUN] = "completed"
        state = self.watch.tick()
        self.assertEqual(len(self.events(state, "overdue")), 1)
        self.assertEqual(len(self.events(state, "completion")), 1)

    def test_untrusted_marker_does_not_suppress_delivery(self):
        self.gateway.responses[RUN] = "completed"
        eid = w.event_id("example", RUN, "completion", 1)
        FakeGitHub.comments_store.append({"id": 99, "body": w.marker(eid) + "\nspoofed",
                                          "user": {"login": "untrusted"}})
        self.watch.tick()
        self.assertEqual(len(FakeGitHub.posts), 1)

    def test_bounded_retry_exhaustion_and_read_only_reconciliation(self):
        self.gateway.responses[RUN] = "failed"
        FakeGitHub.fail_after_post = True
        e, = self.events(self.watch.tick(), "failed")
        # Force all remote comments absent to model lost POSTs; bounded five attempts.
        for _ in range(6):
            FakeGitHub.comments_store = []
            self.clock.now += 4000
            self.watch.tick()
        self.assertEqual(len(FakeGitHub.posts), 5)
        e, = self.events(self.store.load(), "failed")
        self.assertEqual(e["delivery"], "exhausted")
        FakeGitHub.comments_store = [{"id": 999, "body": FakeGitHub.posts[0], "user": {"login": "example-actor"}}]
        self.watch.reconcile()
        self.assertEqual(self.events(self.store.load(), "failed")[0]["comment_id"], 999)
        self.assertEqual(len(FakeGitHub.posts), 5)

    def test_route_mismatch_and_invalid_run_never_adopted(self):
        with self.assertRaises(ValueError):
            self.watch.register("example", "other/repo", 42, RUN2, "source-11", 5000)
        with self.assertRaises(ValueError):
            self.watch.register("example", *ROUTE, "run_invalid", "source-11", 5000)
        with self.assertRaises(ValueError):
            self.watch.register("example", *ROUTE, RUN, "source-10", 5000)
        self.assertEqual(list(self.store.load()["runs"]), [RUN])
        self.assertEqual(self.gateway.calls, [])

    def test_deadline_requires_explicit_sensible_duration(self):
        with self.assertRaises(ValueError):
            self.watch.register("example", *ROUTE, RUN2, "source-11", 1001)
        self.watch.register("example", *ROUTE, RUN2, "source-11", 8000)
        self.assertEqual(len(self.watch.tick()["runs"]), 2)

    def test_nonfinite_deadlines_rejected_before_mutation(self):
        before = self.store.path.read_bytes()
        for bad in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.watch.register("example", *ROUTE, RUN2, "source-11", bad)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_malformed_persisted_timing_rejected(self):
        baseline = self.store.load()
        baseline["events"]["fictional"] = {
            "created": 1000, "delivery_deadline": 9999,
            "next_attempt": 1000, "ack_deadline": None,
        }
        for path, value in (("deadline", float("nan")), ("deadline", float("inf")),
                            ("deadline", -float("inf")), ("deadline", "bad"), ("deadline", True),
                            ("created", float("nan")), ("delivery_deadline", float("inf")),
                            ("next_attempt", "bad"), ("ack_deadline", -float("inf")),
                            ("ack_deadline", True)):
            with self.subTest(path=path, value=value):
                state = json.loads(json.dumps(baseline))
                target = state["runs"][RUN] if path == "deadline" else state["events"]["fictional"]
                target[path] = value
                self.store.path.write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    self.store.load()
        state = json.loads(json.dumps(baseline))
        state["runs"][RUN]["deadline"] = 4600
        self.store.path.write_text(json.dumps(state).replace('"deadline": 4600', '"deadline": 1e9999'))
        with self.assertRaises(ValueError):
            self.store.load()
        with self.assertRaises(ValueError):
            self.store.save({"deadline": float("nan")})

    def test_wrong_gateway_run_id_is_blocked_not_adopted(self):
        self.gateway.responses[RUN] = {"run_id": RUN2, "status": "completed"}
        state = self.watch.tick()
        self.assertEqual(len(self.events(state, "connection-blocked")), 1)
        self.assertFalse(self.events(state, "completion"))

    def test_ack_correlation_and_no_feedback_loop(self):
        self.gateway.responses[RUN] = "completed"
        e, = self.events(self.watch.tick(), "completion")
        FakeGitHub.comments_store.append({"id": 201, "body": w.ack_marker(e["id"], 999), "user": {"login": "example-actor"}})
        self.watch.tick()
        self.assertEqual(self.events(self.store.load(), "completion")[0]["ack"], "pending")
        FakeGitHub.comments_store.append({"id": 202, "body": w.ack_marker(e["id"], e["comment_id"]), "user": {"login": "untrusted"}})
        self.watch.tick()
        self.assertEqual(self.events(self.store.load(), "completion")[0]["ack"], "pending")
        FakeGitHub.comments_store.append({"id": 203, "body": w.ack_marker(e["id"], e["comment_id"]), "user": {"login": "example-actor"}})
        self.watch.tick()
        self.assertEqual(self.events(self.store.load(), "completion")[0]["ack"], "acknowledged")
        self.assertEqual(len(FakeGitHub.posts), 1)

    def test_ack_timeout_retained_without_storm(self):
        self.gateway.responses[RUN] = "completed"
        e, = self.events(self.watch.tick(), "completion")
        self.clock.now = e["ack_deadline"] + 1
        for _ in range(3):
            self.watch.tick()
        self.assertEqual(self.events(self.store.load(), "completion")[0]["ack"], "overdue")
        self.assertEqual(len(FakeGitHub.posts), 1)

    def test_process_lock_and_atomic_registry(self):
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        with self.store.locked():
            process = ctx.Process(target=competing_lock, args=(self.store.directory, q))
            process.start()
            self.assertEqual(q.get(timeout=5), "blocked")
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.watch.tick()
        self.assertEqual(json.loads(self.store.path.read_text())["version"], 1)
        self.assertFalse(list(self.store.directory.glob(".watchdog-*")))

    def test_no_automatic_job_replay_or_control(self):
        self.gateway.responses[RUN] = "interrupted"
        self.watch.tick()
        self.assertEqual(self.gateway.calls, [RUN])
        self.assertEqual(len(FakeGitHub.posts), 1)
        self.assertNotIn("start", dir(self.gateway))
        self.assertNotIn("stop", dir(self.gateway))

    def test_stable_event_identity(self):
        self.assertEqual(w.event_id("example", RUN, "completion", 1), w.event_id("example", RUN, "completion", 1))
        self.assertNotEqual(w.event_id("example", RUN, "completion", 1), w.event_id("example", RUN, "completion", 2))

    def control_client(self):
        """Exercise the real authenticated handler on an ephemeral loopback port."""
        credential = Path(self.temp.name) / "control-key"
        credential.write_text("fictional-test-key")
        credential.chmod(0o600)
        ready = threading.Event()
        servers = []

        def server_factory(address, handler):
            self.assertEqual(address[0], "127.0.0.1")
            server = HTTPServer(("127.0.0.1", 0), handler)
            servers.append(server)
            ready.set()
            return server

        with patch.object(w, "HTTPServer", side_effect=server_factory):
            thread = threading.Thread(target=w.serve_control,
                args=(self.watch, 12345, credential), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(3), "control server did not bind")
        server = servers[0]
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path, key="fictional-test-key"):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                headers = {} if key is None else {"Authorization": "Bearer " + key}
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        return get

    def test_control_busy_is_503_then_read_recovers_without_mutation(self):
        get = self.control_client()
        before = self.store.path.read_bytes()
        with self.store.locked():
            self.assertEqual(get("/tracked/" + RUN), (503, {"error": "registry busy"}))
        status, tracked = get("/tracked/" + RUN)
        self.assertEqual(status, 200)
        self.assertEqual(tracked, self.watch.tracked(RUN))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(FakeGitHub.posts, [])

    def test_control_missing_auth_and_route_failures_are_not_busy(self):
        get = self.control_client()
        self.assertEqual(get("/tracked/" + RUN2)[0], 404)
        with self.store.locked():
            self.assertEqual(get("/tracked/" + RUN, key=None)[0], 401)
            self.assertEqual(get("/tracked/" + RUN, key="wrong")[0], 401)
            self.assertEqual(get("/tracked/not-a-run")[0], 404)
        self.watch.routes = {}
        self.assertEqual(get("/tracked/" + RUN)[0], 404)

    def test_real_adapter_paginated_read_parses_all_lines(self):
        rows = [{"id": i, "body": "fixture", "user": {"login": "example-actor"}} for i in range(101)]
        with patch.object(w.subprocess, "run", return_value=subprocess.CompletedProcess([], 0,
                          "\n".join(json.dumps(r) for r in rows), "")) as call:
            self.assertEqual(len(w.GitHub(*ROUTE, "example-actor").comments()), 101)
        self.assertIn("--paginate", call.call_args.args[0])

    def test_real_adapter_refuses_issue_number_or_different_actor_before_post(self):
        for outputs in (("0", "example-actor"), ("42", "other")):
            with self.subTest(outputs=outputs), patch.object(w.subprocess, "run",
                    side_effect=[subprocess.CompletedProcess([], 0, out, "") for out in outputs]) as call:
                with self.assertRaises(ValueError):
                    w.GitHub(*ROUTE, "example-actor").post("fictional")
                self.assertEqual(call.call_count, 1 if outputs[0] == "0" else 2)


if __name__ == "__main__":
    unittest.main()

