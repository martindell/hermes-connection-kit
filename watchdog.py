"""Opt-in, local, pull-only run observer and GitHub PR-comment outbox.

No task submission/control endpoint is implemented. The only gateway operation is
GET /v1/runs/{run_id}; GitHub writes are limited to a configured PR's comments.
"""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import contextlib
import fcntl
import grp
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPHandler

RUN_ID = re.compile(r"run_[0-9a-f]{32}\Z")
PROJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE = {"queued", "running", "stopping", "waiting_for_approval", "unknown"}
KINDS = {"completion", "failed", "waiting-for-approval", "overdue", "connection-blocked"}
MAX_RESPONSE = 262144
MAX_ACTIVE_RUNS = 32
# Reserve five seconds for registry I/O and process scheduling. Individual network
# calls have their own timeouts; this is an admission budget, not a hard OS deadline.
TICK_BUDGET = 39
PHASE_COST = (9, 26, 8)  # tunnel + gateway; comments + PR/actor + POST; ack comments
MAX_TICK_OPERATIONS = 64
TICK_LOCK_WAIT = 5.0


def _finite_time(value):
    return type(value) in (int, float) and math.isfinite(value)


def _reject_json_constant(value):
    raise ValueError("non-finite JSON constant in private state")


def _finite_json_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _safe_ancestors(path):
    for parent in Path(path).parents:
        if parent == Path('/'):
            continue
        s = parent.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_mode & 0o002:
            raise ValueError("unsafe ancestor")
        if s.st_mode & 0o020:
            members = {p.pw_uid for p in pwd.getpwall() if p.pw_gid == s.st_gid}
            members.update(pwd.getpwnam(n).pw_uid for n in grp.getgrgid(s.st_gid).gr_mem)
            if members - {os.getuid()}:
                raise ValueError("shared writable ancestor")


def _private_dir(path):
    path = Path(path)
    _safe_ancestors(path)
    if path.is_symlink():
        raise ValueError("state directory must not be a symlink")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    s = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError("state directory must be owner-only")
    return path


def _private_read(path, limit=MAX_RESPONSE):
    _safe_ancestors(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_nlink != 1:
            raise ValueError("credential/state file must be owner-only regular file")
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError("private file exceeds limit")
        return data
    finally:
        os.close(fd)


def _atomic(path, data):
    parent = _private_dir(Path(path).parent)
    fd, temp = tempfile.mkstemp(prefix=".watchdog-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        if Path(path).is_symlink():
            raise ValueError("state target must not be a symlink")
        os.replace(temp, path)
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Store:
    def __init__(self, directory):
        self.directory = _private_dir(directory)
        self.path = self.directory / "registry.json"

    @contextlib.contextmanager
    def locked(self, *, wait_seconds=0):
        # Control requests remain fail-fast. Only scheduled ticks opt into a
        # short wait, so coincident timers need not lose an entire tick.
        if not _finite_time(wait_seconds) or not 0 <= wait_seconds <= TICK_LOCK_WAIT:
            raise ValueError("invalid lock wait")
        deadline = time.monotonic() + wait_seconds
        fd = os.open(self.directory / "registry.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
                raise ValueError("unsafe lock")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(0.05, remaining))
            yield
        finally:
            os.close(fd)

    def load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {"version": 1, "enabled": False, "runs": {}, "events": {}}
        data = json.loads(_private_read(self.path, limit=16 * 1024 * 1024),
                          parse_constant=_reject_json_constant, parse_float=_finite_json_float)
        if data.get("version") != 1 or not isinstance(data.get("runs"), dict) or not isinstance(data.get("events"), dict):
            raise ValueError("invalid registry version/shape")
        for run in data["runs"].values():
            if not isinstance(run, dict) or not _finite_time(run.get("deadline")):
                raise ValueError("invalid persisted run deadline")
        for event in data["events"].values():
            if (not isinstance(event, dict)
                    or any(not _finite_time(event.get(key)) for key in ("created", "delivery_deadline", "next_attempt"))
                    or (event.get("ack_deadline") is not None and not _finite_time(event["ack_deadline"]))):
                raise ValueError("invalid persisted event timing")
        return data

    def save(self, data):
        _atomic(self.path, json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


class Gateway:
    def __init__(self, origin, credential_file):
        u = urlsplit(origin)
        if u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port or u.path or u.query or u.fragment or u.username:
            raise ValueError("gateway origin must be a literal loopback HTTP host:port")
        self.origin = origin.rstrip("/")
        self.credential_file = credential_file

    def status(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run ID")
        token = _private_read(self.credential_file, limit=16384).strip()
        if not token or b"\n" in token or b"\r" in token or b" " in token:
            raise ValueError("invalid local gateway credential")
        try:
            credential = token.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid local gateway credential encoding") from exc
        request = Request(self.origin + "/v1/runs/" + run_id,
                          headers={"Authorization": "Bearer " + credential, "Accept": "application/json"})
        # No proxy/redirect: the bearer must not leave the literal loopback endpoint.
        from urllib.request import ProxyHandler, HTTPRedirectHandler
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("gateway redirect refused")
        with build_opener(ProxyHandler({}), HTTPHandler(), NoRedirect()).open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError("gateway status unavailable")
            raw = response.read(MAX_RESPONSE + 1)
            # read(n) does not raise on a short Content-Length; reject a clean
            # JSON prefix too, rather than treating a truncated reply as status.
            if response.length not in (None, 0):
                raise IncompleteRead(b"", response.length)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("gateway response too large")
        return json.loads(raw, parse_constant=_reject_json_constant, parse_float=_finite_json_float)


class GitHub:
    """Uses an already-authenticated gh CLI; never accepts a token in config or argv."""
    def __init__(self, repo, pr, actor):
        if (not REPO.fullmatch(repo) or any(x in {".", ".."} for x in repo.split("/"))
                or type(pr) is not int or pr < 1 or not re.fullmatch(r"[A-Za-z0-9-]+", actor)):
            raise ValueError("invalid GitHub PR route")
        self.endpoint = f"repos/{repo}/issues/{pr}/comments"
        self.repo, self.pr, self.actor = repo, pr, actor

    def _check_destination(self):
        # Issue and PR comments share an API. Confirm the exact PR and actor.
        def get(endpoint):
            return subprocess.run(["gh", "api", endpoint, "--jq", ".number // .login"],
                                  capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        if get(f"repos/{self.repo}/pulls/{self.pr}") != str(self.pr) or get("user") != self.actor:
            raise ValueError("GitHub PR or publishing actor mismatch")

    def comments(self):
        result = subprocess.run(["gh", "api", self.endpoint + "?per_page=100", "--paginate", "--jq", ".[] | {id,body,user:{login:.user.login}}"],
                                capture_output=True, text=True, timeout=8, check=True)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def post(self, body):
        self._check_destination()
        fd, name = tempfile.mkstemp(prefix=".watchdog-post-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"body": body}, f)
            result = subprocess.run(["gh", "api", "--method", "POST", self.endpoint, "--input", name, "--jq", ".id"],
                                    capture_output=True, text=True, timeout=8, check=True)
            return int(result.stdout.strip())
        finally:
            os.unlink(name)


def event_id(project, run_id, kind, generation):
    raw = json.dumps([project, run_id, kind, generation], separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def marker(eid):
    return "<!-- hermes-watchdog:event:" + eid + " -->"


def ack_marker(eid, comment_id):
    return f"<!-- hermes-watchdog:ack:{eid}:{comment_id} -->"


class ManagedTunnel:
    """Read-only systemd observation of this exact managed tunnel unit."""
    def __init__(self, unit, fragment):
        if (not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]+[.]service', unit)
                or not isinstance(fragment, str) or not fragment.startswith('/')
                or not re.fullmatch(r'/[A-Za-z0-9_./+-]+', fragment)):
            raise ValueError('invalid managed tunnel identity')
        self.unit, self.fragment = unit, fragment

    def healthy(self):
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', self.unit,
                                 '--property=ActiveState', '--property=FragmentPath'],
                                capture_output=True, text=True, timeout=4, check=True)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return fields.get('ActiveState') == 'active' and fields.get('FragmentPath') == self.fragment


def _next_key(keys, cursor):
    """Resume after the last selected key, even when membership changes.

    Accept the former numeric cursor once when upgrading an existing registry.
    """
    if type(cursor) is int:
        return keys[cursor % len(keys)]
    if isinstance(cursor, str):
        return next((key for key in keys if key > cursor), keys[0])
    raise ValueError("invalid persisted cursor")


class Watchdog:
    def __init__(self, store, routes, gateway, github_factory=GitHub, clock=time.time,
                 ack_actor="example-actor", tunnel=None, monotonic=time.monotonic):
        # routes: project -> (exact repo, exact PR); never a wildcard or arbitrary target.
        if not routes or any(not PROJECT.fullmatch(p) or not REPO.fullmatch(r) or type(n) is not int or n < 1
                             or any(x in {".", ".."} for x in r.split("/"))
                             for p, (r, n) in routes.items()):
            raise ValueError("invalid allowlisted route")
        if not re.fullmatch(r"[A-Za-z0-9-]+", ack_actor):
            raise ValueError("invalid acknowledgement actor")
        self.store, self.routes, self.gateway = store, routes, gateway
        self.github_factory, self.clock, self.ack_actor = github_factory, clock, ack_actor
        self.tunnel, self.monotonic = tunnel, monotonic

    def register(self, project, repo, pr, run_id, correlation, deadline_at):
        if (project not in self.routes or self.routes[project] != (repo, pr)
                or not RUN_ID.fullmatch(run_id) or not isinstance(correlation, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", correlation)
                or not _finite_time(deadline_at) or deadline_at < self.clock() + 600):
            raise ValueError("invalid route, returned ID, correlation or per-job deadline")
        with self.store.locked():
            state = self.store.load()
            if run_id in state["runs"]:
                raise ValueError("run already registered; no adoption/replay")
            if sum(r["observed"] not in TERMINAL for r in state["runs"].values()) >= MAX_ACTIVE_RUNS:
                raise ValueError("active run capacity reached; reconcile before registering")
            state["runs"][run_id] = {"project": project, "repo": repo, "pr": pr,
                                     "correlation": correlation, "deadline": deadline_at,
                                     "observed": "", "blocked": False, "generations": {}, "overdue": False}
            self.store.save(state)

    def set_enabled(self, enabled):
        with self.store.locked():
            state = self.store.load()
            state["enabled"] = bool(enabled)
            self.store.save(state)

    def tracked(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run ID")
        with self.store.locked():
            state = self.store.load()
            run = state["runs"].get(run_id)
            if run is None or self.routes.get(run["project"]) != (run["repo"], run["pr"]):
                raise ValueError("run not registered on an allowed route")
            return {"run_id": run_id, "project": run["project"], "repo": run["repo"],
                    "pr": run["pr"], "correlation": run["correlation"],
                    "deadline": run["deadline"], "observed": run["observed"],
                    "blocked": run["blocked"], "enabled": state["enabled"]}


    def _event(self, state, run_id, kind, now, result=None):
        run = state["runs"][run_id]
        gen = run["generations"].get(kind, 0) + 1
        run["generations"][kind] = gen
        eid = event_id(run["project"], run_id, kind, gen)
        event = {"id": eid, "run_id": run_id, "project": run["project"], "repo": run["repo"],
                 "pr": run["pr"], "correlation": run["correlation"], "kind": kind,
                 "generation": gen, "created": now, "delivery_deadline": now + 86400,
                 "attempts": 0, "next_attempt": now, "delivery": "pending", "comment_id": None,
                 "ack": "pending", "ack_deadline": None}
        state["events"][eid] = event
        if result is not None:
            # The status payload can contain private output. Keep it ONLY in the private state
            # directory and do not forward output/error/approval details to GitHub.
            _atomic(self.store.directory / ("result-" + eid + ".json"),
                    json.dumps({"run_id": run_id, "correlation": run["correlation"], "status": result},
                               sort_keys=True, allow_nan=False).encode())
        return event

    def _observe(self, state, now, run_ids=None):
        for run_id in (run_ids if run_ids is not None else state["runs"]):
            run = state["runs"][run_id]
            if run["project"] not in self.routes or self.routes[run["project"]] != (run["repo"], run["pr"]):
                continue  # fail closed on config drift; do not query or deliver another route
            if run["observed"] in TERMINAL:
                continue
            try:
                response = self.gateway.status(run_id)
                if not isinstance(response, dict) or response.get("run_id") != run_id:
                    raise ValueError("gateway returned mismatched run ID")
                status = response.get("status")
                if not isinstance(status, str) or status not in ACTIVE | TERMINAL:
                    raise ValueError("gateway returned unrecognised status")
            except (OSError, HTTPException, ValueError, KeyError, TimeoutError):
                # No raw exception/log: transport errors can contain URL or auth context.
                if not run["blocked"]:
                    run["blocked"] = True
                    self._event(state, run_id, "connection-blocked", now)
                continue
            run["blocked"] = False
            previous = run["observed"]
            if status != previous:
                run["observed"] = status
                if status == "waiting_for_approval":
                    self._event(state, run_id, "waiting-for-approval", now)
                elif status in TERMINAL:
                    self._event(state, run_id, "completion" if status == "completed" else "failed", now, response)
            # HTTP 200, updated_at and last_event are NOT reliable progress heartbeats.
            # Only the explicit per-job deadline establishes an overdue notification.
            if status not in TERMINAL and status != "waiting_for_approval" and now >= run["deadline"] and not run["overdue"]:
                run["overdue"] = True
                self._event(state, run_id, "overdue", now)

    @staticmethod
    def _body(event):
        return (marker(event["id"]) + "\nHermes watchdog: " + event["kind"] + "\n"
                + "Project: " + event["project"] + "\nRun: " + event["run_id"]
                + "\nCorrelation: " + event["correlation"]
                + "\nResult: retained privately on Hermes host; fetch the known run ID via the approved status route.\n"
                + "Delivery is not Work acknowledgement; do not dispatch from this comment.")

    def _deliver(self, state, now, event_ids=None):
        for eid in (event_ids if event_ids is not None else state["events"]):
            event = state["events"][eid]
            if event["delivery"] not in {"pending", "exhausted"}:
                continue
            if event["project"] not in self.routes or self.routes[event["project"]] != (event["repo"], event["pr"]):
                continue
            # Even exhausted events can be reconciled read-only after an ambiguous POST.
            if event["delivery"] == "exhausted" or now < event["next_attempt"]:
                continue
            gh = self.github_factory(event["repo"], event["pr"], self.ack_actor)
            attempt_started = False
            try:
                comments = gh.comments()  # complete pagination required before EVERY post/retry
                matches = [c for c in comments if c.get("user", {}).get("login") == self.ack_actor
                           and c.get("body", "").startswith(marker(event["id"]) + "\n")
                           and c.get("body", "").count(marker(event["id"])) == 1]
                if len(matches) == 1:
                    event.update(delivery="delivered", comment_id=matches[0]["id"],
                                 ack_deadline=now + 86400)
                    self.store.save(state)
                    continue
                if matches:
                    event["delivery"] = "exhausted"  # ambiguous duplicate; manual review
                    self.store.save(state)
                    continue
                if event["attempts"] >= 5 or now >= event["delivery_deadline"]:
                    event["delivery"] = "exhausted"
                    self.store.save(state)
                    continue
                event["attempts"] += 1
                attempt_started = True
                event["next_attempt"] = now + min(60 * 2 ** (event["attempts"] - 1), 3600)
                self.store.save(state)  # durable before uncertain POST
                comment_id = gh.post(self._body(event))
                if type(comment_id) is not int or comment_id < 1:
                    raise ValueError("ambiguous comment response")
                event.update(delivery="delivered", comment_id=comment_id, ack_deadline=now + 86400)
                self.store.save(state)
            except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
                # A failed GET is not evidence that a POST is safe. No exception content logged.
                if not attempt_started:
                    event["attempts"] += 1
                if event["attempts"] >= 5 or now >= event["delivery_deadline"]:
                    event["delivery"] = "exhausted"
                else:
                    event["next_attempt"] = now + min(60 * 2 ** max(event["attempts"], 1), 3600)
                self.store.save(state)

    def _ack(self, state, now, route=None):
        grouped = {}
        for event in state["events"].values():
            if event["delivery"] == "delivered" and event["ack"] != "acknowledged":
                if event["project"] in self.routes and self.routes[event["project"]] == (event["repo"], event["pr"]):
                    grouped.setdefault((event["repo"], event["pr"]), []).append(event)
        for (repo, pr), events in grouped.items():
            if route is not None and (repo, pr) != route:
                continue
            try:
                comments = self.github_factory(repo, pr, self.ack_actor).comments()
            except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
                continue
            for event in events:
                exact = ack_marker(event["id"], event["comment_id"])
                if any(c.get("user", {}).get("login") == self.ack_actor and
                       c.get("body", "").strip() == exact for c in comments):
                    event["ack"] = "acknowledged"
                elif now >= event["ack_deadline"]:
                    event["ack"] = "overdue"  # retained for manual/backstop; NO auto re-post
        self.store.save(state)

    def tick(self):
        """Complete offline/manual cycle; the timer uses bounded_tick instead."""
        with self.store.locked():
            state = self.store.load()
            if not state['enabled']:
                return state
            now = self.clock()
            self._observe(state, now)
            self.store.save(state)
            self._deliver(state, now)
            self._ack(state, now)
            return state

    def bounded_tick(self):
        """Batch fast work; rotate phase priority and admit only calls that fit.

        Monotonic elapsed time is charged after every call. Each next call must
        fit its worst configured I/O timeout plus five seconds of headroom.
        OS stalls, disk fsync and scheduling are not hard real-time bounded.
        """
        started = self.monotonic()
        with self.store.locked(wait_seconds=TICK_LOCK_WAIT):
            state = self.store.load()
            if not state["enabled"]:
                return state  # no alarms, network or delivery while paused/disconnected
            now = self.clock()
            first = state.get('tick_phase', 0)
            if type(first) is not int or first not in (0, 1, 2):
                raise ValueError('invalid tick phase')
            state['tick_phase'] = (first + 1) % 3
            self.store.save(state)  # rotation survives a killed/ambiguous operation
            seen = (set(), set(), set())
            operations = 0
            # Each visit makes one call at most, then tries the other phases.
            # A slow observation cannot monopolise delivery or acknowledgement
            # across ticks: the starting phase rotates durably on every call.
            while operations < MAX_TICK_OPERATIONS:
                progressed = False
                for offset in range(3):
                    phase = (first + offset) % 3
                    if operations >= MAX_TICK_OPERATIONS or self.monotonic() - started + PHASE_COST[phase] > TICK_BUDGET:
                        continue
                    if phase == 0:
                        ids = sorted(run_id for run_id, run in state['runs'].items()
                                     if run_id not in seen[0] and run['observed'] not in TERMINAL
                                     and self.routes.get(run['project']) == (run['repo'], run['pr']))
                        if not ids:
                            continue
                        run_id = _next_key(ids, state.get('run_cursor', 0))
                        seen[0].add(run_id)
                        state['run_cursor'] = run_id
                        self.store.save(state)  # selection survives a slow/killed GET
                        run = state['runs'][run_id]
                        try:
                            healthy = self.tunnel is None or self.tunnel.healthy()
                        except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
                            healthy = False
                        if not healthy:
                            if not run['blocked']:
                                run['blocked'] = True
                                self._event(state, run_id, 'connection-blocked', now)
                        else:
                            self._observe(state, now, [run_id])
                    elif phase == 1:
                        ids = sorted(eid for eid, e in state['events'].items()
                                     if eid not in seen[1] and e['delivery'] == 'pending'
                                     and now >= e['next_attempt']
                                     and self.routes.get(e['project']) == (e['repo'], e['pr']))
                        if not ids:
                            continue
                        eid = _next_key(ids, state.get('event_cursor', 0))
                        seen[1].add(eid)
                        state['event_cursor'] = eid
                        self.store.save(state)  # observations and cursor durable before POST
                        self._deliver(state, now, [eid])
                    else:
                        routes = sorted({(e['repo'], e['pr']) for e in state['events'].values()
                                         if e['delivery'] == 'delivered' and e['ack'] != 'acknowledged'
                                         and self.routes.get(e['project']) == (e['repo'], e['pr'])})
                        routes = [r for r in routes if r not in seen[2]]
                        if not routes:
                            continue
                        keyed_routes = {f'{repo}#{pr}': (repo, pr) for repo, pr in routes}
                        key = _next_key(sorted(keyed_routes), state.get('ack_cursor', 0))
                        route = keyed_routes[key]
                        seen[2].add(route)
                        state['ack_cursor'] = key
                        self.store.save(state)  # route selection survives an interrupted GET
                        self._ack(state, now, route)
                    operations += 1
                    progressed = True
                    self.store.save(state)
                if not progressed:
                    break
            return state

    def reconcile(self):
        """Read-only remote reconciliation for exhausted/ambiguous delivery; no POST."""
        with self.store.locked():
            state = self.store.load()
            if not state["enabled"]:
                return state
            for event in state["events"].values():
                if event["delivery"] == "delivered" or event["project"] not in self.routes or self.routes[event["project"]] != (event["repo"], event["pr"]):
                    continue
                comments = self.github_factory(event["repo"], event["pr"], self.ack_actor).comments()
                matches = [c for c in comments if c.get("user", {}).get("login") == self.ack_actor
                           and c.get("body", "").startswith(marker(event["id"]) + "\n")
                           and c.get("body", "").count(marker(event["id"])) == 1]
                if len(matches) == 1:
                    event.update(delivery="delivered", comment_id=matches[0]["id"], ack_deadline=self.clock() + 86400)
            self._ack(state, self.clock())
            self.store.save(state)
            return state


def serve_control(watchdog, port, credential_file):
    """Authenticated loopback registration/read adapter; never task dispatch."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid control port")
    token = _private_read(credential_file, 16384).strip()
    if not token or any(c < 33 or c > 126 for c in token):
        raise ValueError("invalid control credential")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass

        def reply(self, status, payload):
            data = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            supplied = self.headers.get("Authorization", "").encode("ascii", "ignore")
            return hmac.compare_digest(supplied, b"Bearer " + token)

        def do_GET(self):
            if not self.authorized():
                self.reply(401, {"error": "unauthorized"})
                return
            if self.path == "/health":
                self.reply(200, {"ready": True})
                return
            m = re.fullmatch(r"/tracked/(run_[0-9a-f]{32})", self.path)
            if not m:
                self.reply(404, {"error": "not found"})
                return
            try:
                self.reply(200, watchdog.tracked(m[1]))
            except BlockingIOError:
                # A reader can collide with an observer/recovery tick. This is
                # temporary contention, not evidence that registration is absent.
                self.reply(503, {"error": "registry busy"})
            except ValueError:
                self.reply(404, {"error": "not registered or unavailable"})

        def do_POST(self):
            if not self.authorized():
                self.reply(401, {"error": "unauthorized"})
                return
            if self.path not in ("/register", "/register?mode=register"):
                self.reply(404, {"error": "not found"})
                return
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or not 0 < int(length) <= 2048:
                self.reply(413, {"error": "invalid body length"})
                return
            try:
                payload = json.loads(self.rfile.read(int(length)),
                                     parse_constant=_reject_json_constant,
                                     parse_float=_finite_json_float)
                if not isinstance(payload, dict) or set(payload) != {
                        "project", "repo", "pr", "run_id", "correlation", "deadline_at"}:
                    raise ValueError("invalid registration fields")
                watchdog.register(**payload)
                self.reply(201, {"run_id": payload["run_id"], "registered": True})
            except (ValueError, TypeError, BlockingIOError, OSError):
                self.reply(400, {"error": "invalid or duplicate registration; read tracked status before any decision"})

    server = HTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opt-in callback watchdog (no start/stop API)")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--config", required=True, help="owner-only JSON with routes, gateway and credential path")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("register", help="register ONLY a returned MCP start run ID")
    for key in ("project", "repo", "run-id", "correlation"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--deadline-at", type=float, required=True, help="absolute Unix UTC seconds; set per job")
    for action in ("enable", "pause", "tick", "reconcile", "run"):
        sub.add_parser(action)
    control = sub.add_parser("serve-control")
    control.add_argument("--port", type=int, required=True)
    control.add_argument("--credential-file", required=True)
    args = parser.parse_args(argv)
    cfg = json.loads(_private_read(args.config))
    if set(cfg) != {"routes", "gateway_origin", "gateway_credential_file", "ack_actor", "control_port", "managed_unit", "managed_fragment"}:
        raise ValueError("invalid configuration keys")
    if not isinstance(cfg["routes"], dict) or any(
            not isinstance(v, dict) or set(v) != {"repo", "pr"} for v in cfg["routes"].values()):
        raise ValueError("invalid route configuration")
    routes = {p: (v["repo"], v["pr"]) for p, v in cfg["routes"].items()}
    watchdog = Watchdog(Store(args.state_dir), routes,
                        Gateway(cfg["gateway_origin"], cfg["gateway_credential_file"]),
                        ack_actor=cfg["ack_actor"],
                        tunnel=ManagedTunnel(cfg["managed_unit"], cfg["managed_fragment"]))
    if args.command == "register":
        watchdog.register(args.project, args.repo, args.pr, args.run_id, args.correlation, args.deadline_at)
    elif args.command in ("enable", "pause"):
        watchdog.set_enabled(args.command == "enable")
    elif args.command == "run":
        # Explicit foreground opt-in only; no boot unit, no hidden retries or daemon install.
        while True:
            watchdog.bounded_tick()
            time.sleep(60)
    elif args.command == "serve-control":
        serve_control(watchdog, args.port, args.credential_file)
    else:
        watchdog.bounded_tick() if args.command == "tick" else watchdog.reconcile()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
