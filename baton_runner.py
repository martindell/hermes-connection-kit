"""Independent, outbound-only GitHub baton reader for Unix hosts (Python 3.11+).

One invocation is one check. No job control or GitHub writes. Default tick is
dry-run; --notify explicitly enables notification submission.
"""
import argparse
import copy
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager

# Isolated Python deliberately omits the script directory. Only load the
# installed adjacent modules; never the working directory or PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import baton
import baton_notify

MARKER = "<!-- hermes-baton:v1 -->"
LIMIT = 4 * 1024 * 1024
ERROR_DIRECTORY = None


@contextmanager
def read_deadline(seconds=40):
    """Bound DNS/connect/read wall time in this standalone Unix process."""
    def expired(signum, frame):
        raise TimeoutError("journal wall deadline exceeded")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("non-finite JSON")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("file is not owner-private")
        raw = handle.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("file exceeds limit")
        return raw


def atomic(path, data):
    raw = baton.canonical(data).encode()
    if len(raw) > LIMIT:
        raise ValueError("state exceeds limit")
    fd, name = tempfile.mkstemp(prefix=".baton-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class GitHub:
    def __init__(self, repo, issue, token, opener=None, clock=time.monotonic):
        if (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo)
                or repo.split("/")[1] in {".", ".."}
                or type(issue) is not int or issue < 1):
            raise ValueError("invalid route")
        if not token or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid token")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        self.url = f"https://api.github.com/repos/{repo}/issues/{issue}"
        self.token, self.clock = token, clock

    def comments(self):
        start, total, result = self.clock(), 0, []
        for page in range(1, 21):
            remaining = 30 - (self.clock() - start)
            if remaining <= 0:
                raise TimeoutError("journal budget exhausted")
            request = urllib.request.Request(self.url + f"/comments?per_page=100&page={page}",
                headers={"Authorization": "Bearer " + self.token,
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "hermes-baton-watchdog"})
            with self.opener.open(request, timeout=min(8, remaining)) as response:
                if response.status != 200:
                    raise ValueError("journal unavailable")
                raw = response.read(LIMIT - total + 1)
            total += len(raw)
            if total > LIMIT:
                raise ValueError("journal exceeds limit")
            items = decode(raw)
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError("invalid page")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("journal page limit reached; rotate journal explicitly")


def snapshot(comments, config, session):
    """Authenticate scope and timestamp; claimed actor is not a separate identity."""
    result, ids = [], set()
    expected = f"https://api.github.com/repos/{config['repo']}/issues/{config['issue']}"
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("invalid comment")
        cid = comment.get("id")
        if type(cid) is not int or cid < 1 or cid in ids:
            raise ValueError("invalid or repeated comment ID")
        ids.add(cid)
        if comment.get("issue_url") != expected:
            raise ValueError("wrong journal")
        user = comment.get("user")
        if not isinstance(user, dict):
            raise ValueError("invalid journal author")
        if user.get("login") != config["author"]:
            continue
        body = comment.get("body", "")
        if not isinstance(body, str):
            raise ValueError("invalid comment body")
        if body.split("\n", 1)[0] != MARKER:
            continue
        record = decode(body.split("\n", 1)[1])
        if not isinstance(record, dict):
            raise ValueError("invalid baton record")
        if record.get("session") != session:
            continue
        record = dict(record)
        del record["session"]
        if "at" in record:
            raise ValueError("journal supplies timestamp")
        created = comment.get("created_at")
        if not isinstance(created, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created):
            raise ValueError("invalid journal timestamp")
        if comment.get("updated_at") != created:
            raise ValueError("baton records must not be edited")
        record["at"] = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        result.append((cid, record, baton.digest({"body": body, "created": created})))
    return sorted(result)


def reconcile(state, comments, config):
    records = snapshot(comments, config, state["session"])
    hashes = {str(cid): fingerprint for cid, _, fingerprint in records}
    if any(hashes.get(cid) != value for cid, value in state["hashes"].items()):
        raise ValueError("accepted journal record removed or changed")
    # The saved core and immutable comment ledger are redundant witnesses.
    # Truncating hashes must not silently erase a previously accepted task.
    prior = baton.empty(config["project"])
    for cid, record, _ in records:
        if str(cid) in state["hashes"]:
            prior = baton.apply(prior, record)
    if prior != state["core"]:
        raise ValueError("saved history and core disagree")
    core = baton.empty(config["project"])
    for _, record, _ in records:
        core = baton.apply(core, record)
    new = copy.deepcopy(state)
    new.update(core=core, hashes=hashes)
    return new


def check(state, config, get_comments, now):
    """Read failures preserve last known state. Returned fault details are generic."""
    try:
        new = reconcile(state, get_comments(), config)
        new["last_ok"] = now
        new["fault"] = False
        return new, baton.due(new["core"], now), False
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        new = copy.deepcopy(state)
        if not state["fault"]:
            new["fault_generation"] += 1
        new["fault"] = True
        incident = {"project": config["project"], "task": "monitor",
                    "owner": "work", "kind": "monitoring-unavailable",
                    "seq": new["fault_generation"]}
        incident["id"] = baton.digest(incident)
        # Do not claim an old obligation is still overdue when a later update
        # may be unreadable. The monitor fault explicitly reports uncertainty.
        return new, [incident], True


def main():
    global ERROR_DIRECTORY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("action", choices=("init", "tick", "status"))
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    directory = args.directory
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("private state directory required")
    if args.notify:
        ERROR_DIRECTORY = directory
    lockfd = os.open(directory / "runner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lockfd)
        print("Another check holds the lock; skipped.")
        return
    try:
        config = decode(private_read(directory / "config.json"))
        if set(config) != {"repo", "issue", "project", "author"}:
            raise ValueError("invalid config")
        baton.identifier(config["project"])
        if not isinstance(config["author"], str) or not re.fullmatch(r"[A-Za-z0-9-]+", config["author"]):
            raise ValueError("invalid author")
        binding = baton.digest(config)
        path = directory / "state.json"
        if args.action == "init":
            if path.exists() or path.is_symlink() or (directory / "alerts.json").exists():
                raise ValueError("state already exists; refusing reinitialisation")
            token = private_read(directory / "github-token").decode().strip()
            with read_deadline():
                GitHub(config["repo"], config["issue"], token).comments()
            state = {"version": 1, "binding": binding, "session": secrets.token_hex(16),
                     "core": baton.empty(config["project"]), "hashes": {},
                     "fault": False, "fault_generation": 0, "last_ok": time.time()}
            atomic(directory / "alerts.json", {})
            atomic(path, state)
            print(baton.canonical({"session": state["session"], "initialised": True,
                                   "monitoring_started": False}))
            return
        state = decode(private_read(path))
        if (state["version"] != 1 or state["binding"] != binding
                or not re.fullmatch(r"[a-f0-9]{32}", state["session"])
                or state["core"]["project"] != config["project"]):
            raise ValueError("state binding mismatch")
        if args.action == "status":
            print(baton.canonical({"session": state["session"], "last_ok": state["last_ok"],
                  "fault": state["fault"], "tasks": state["core"]["tasks"]}))
            return
        def fetch():
            token = private_read(directory / "github-token").decode().strip()
            with read_deadline():
                return GitHub(config["repo"], config["issue"], token).comments()
        new, incidents, failed = check(state, config, fetch, time.time())
        atomic(path, new)
        receipts = decode(private_read(directory / "alerts.json"))
        baton_notify.validate_receipts(receipts)
        if args.notify:
            # One submission per tick; pending incidents rotate naturally once
            # the earlier incident has a durable intent/submission receipt.
            pending = next((i for i in incidents if baton_notify.eligible(i, receipts, time.time())), None)
            if pending:
                baton_notify.submit(pending, receipts,
                    lambda value: atomic(directory / "alerts.json", value))
        print(baton.canonical({"read_ok": not failed, "notify_enabled": args.notify,
                              "incidents": incidents}))
    finally:
        os.close(lockfd)


def local_fault(directory, run=subprocess.run, now=None):
    """Independent, bounded notification receipts when normal JSON is broken.

    Three attempts maximum, 15 minutes apart. Exclusive creation coordinates
    concurrent failures after the main state lock has been released. Keep old
    one-shot receipts suppressed; never erase fault receipts automatically.
    """
    now = time.time() if now is None else baton.timestamp(now)
    if (directory / "monitor-fault.intent").exists() or (directory / "monitor-fault.submitted").exists():
        return
    for attempt in range(1, 4):
        path = directory / f"monitor-fault.{attempt}.intent"
        try:
            info = path.lstat()
        except FileNotFoundError:
            if attempt > 1 and now < (directory / f"monitor-fault.{attempt-1}.intent").lstat().st_mtime + 900:
                return
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write("Local monitor fault; notification outcome may be uncertain.\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(path, (now, now), follow_symlinks=False)
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                run(baton_notify.notification_command("Hermes watchdog fault",
                    "The watchdog check failed locally. Monitoring needs attention; no jobs were changed."), timeout=15, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                atomic(directory / "monitor-fault.submitted", {"attempt": attempt})
            except (OSError, subprocess.SubprocessError):
                return
            return
        else:
            if not stat.S_ISREG(info.st_mode):
                return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if ERROR_DIRECTORY is not None:
            try:
                local_fault(ERROR_DIRECTORY)
            except Exception:
                pass
        # Never emit a credential-bearing exception or raw remote response.
        print("Monitor check failed; inspect private configuration/state. No job was changed.")
        raise SystemExit(1)
