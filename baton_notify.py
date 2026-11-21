"""Watchdog notification adapter; caller supplies a locked, durable receipt store.

Not a standalone runner. No network or service control. Ambiguous submissions
have bounded spaced retries: submission does not prove mail delivery.
"""
import os
import subprocess
import time

import baton

NOTIFY = "/usr/local/emhttp/webGui/scripts/notify"


def notification_command(subject, description):
    """A portable hook receives two arguments. The default uses Unraid."""
    hook = os.environ.get("HERMES_BATON_NOTIFY")
    if hook is None:
        return [NOTIFY, "-e", "Unraid Server Notice", "-s", subject,
                "-d", description, "-i", "warning"]
    if not os.path.isabs(hook):
        raise OSError("notification hook must be an absolute executable path")
    return [hook, subject, description]


def validate_receipts(receipts):
    if not isinstance(receipts, dict):
        raise ValueError("invalid alert receipts")
    for key, value in receipts.items():
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid receipt identity")
        if isinstance(value, str) and value in {"intent", "submitted", "uncertain"}:
            continue  # Old one-shot receipts remain suppressed.
        if (not isinstance(value, dict) or set(value) != {"status", "attempts", "next_attempt"}
                or value["status"] not in ("intent", "submitted", "uncertain")
                or type(value["attempts"]) is not int or not 1 <= value["attempts"] <= 3):
            raise ValueError("invalid receipt")
        baton.timestamp(value["next_attempt"])


def eligible(incident, receipts, now):
    prior = receipts.get(incident["id"])
    if prior is None:
        return True
    if isinstance(prior, str):  # Never replay an older prototype receipt.
        return False
    return (prior["status"] != "submitted" and prior["attempts"] < 3
            and now >= prior["next_attempt"])


def submit(incident, receipts, save, run=subprocess.run, now=None):
    """Call under the caller's exclusive state lock; save must fsync atomically.

    At most three attempts, 15 minutes apart, for an uncertain submission.
    Duplicate email is possible after an ambiguous outcome, but bounded.
    """
    if set(incident) != {"id", "project", "task", "owner", "kind", "seq"}:
        raise ValueError("invalid incident")
    for key in ("project", "task"):
        baton.identifier(incident[key])
    if (incident["owner"] not in ("work", "hermes")
            or incident["kind"] not in ("update-overdue", "handover-unaccepted", "monitoring-unavailable")
            or type(incident["seq"]) is not int or incident["seq"] < 1):
        raise ValueError("invalid incident metadata")
    expected = baton.digest({k: v for k, v in incident.items() if k != "id"})
    if incident["id"] != expected:
        raise ValueError("incident identity mismatch")
    now = time.time() if now is None else baton.timestamp(now)
    if not eligible(incident, receipts, now):
        prior = receipts[expected]
        return prior if isinstance(prior, str) else prior["status"]
    description = (f"Project {incident['project']}, task {incident['task']}: "
                   f"{incident['kind']}; waiting for {incident['owner']}. "
                   "A deadline was missed; no job was restarted.")
    if incident["kind"] == "handover-unaccepted":
        receiver = "hermes" if incident["owner"] == "work" else "work"
        description = (f"Project {incident['project']}, task {incident['task']}: "
                       f"handover acceptance overdue; waiting for {receiver} to accept. "
                       f"{incident['owner']} retains responsibility until acceptance. "
                       "No job was restarted.")
    if incident["kind"] == "monitoring-unavailable":
        description = (f"Project {incident['project']}: watchdog cannot verify the baton journal. "
                       "Task status is unknown; check GitHub access and journal integrity. "
                       "No job was restarted.")
    attempts = receipts.get(expected, {}).get("attempts", 0) + 1
    receipt = {"status": "intent", "attempts": attempts, "next_attempt": now + 900}
    updated = dict(receipts, **{expected: receipt})
    save(updated)  # If persistence fails, no submission is attempted.
    receipts.update(updated)
    try:
        run(notification_command("Hermes watchdog attention", description),
            check=True, timeout=15, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        result = "submitted"
    except (OSError, subprocess.SubprocessError):
        result = "uncertain"
    updated[expected] = dict(receipt, status=result)
    save(updated)
    receipts.update(updated)
    return result
