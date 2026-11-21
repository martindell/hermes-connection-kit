"""Offline baton protocol prototype. No network, job control or service changes.

The adapter must authenticate each record before calling apply(). A GitHub actor
shared by Work and Hermes authenticates the account, not the claimed sender.
"""
import copy
import hashlib
import json
import math
import re

ACTORS = {"work", "hermes", "user"}
ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,119}\Z")


class Conflict(ValueError):
    """Invalid, stale or conflicting record; retain last known obligation."""


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Conflict("invalid identifier")
    return value


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise Conflict("invalid timestamp")
    return value


def evidence(value):
    # Opaque reference only. Never put results, credentials or note contents here.
    if not isinstance(value, str) or not 1 <= len(value) <= 300 or any(
        ord(c) < 32 for c in value
    ):
        raise Conflict("invalid evidence reference")
    return value


def canonical(record):
    try:
        return json.dumps(record, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise Conflict("invalid JSON record") from exc


def digest(record):
    return hashlib.sha256(canonical(record).encode()).hexdigest()


def empty(project):
    return {"version": 1, "project": identifier(project), "tasks": {}, "seen": {}}


def apply(state, record):
    """Return a new state; errors never partially mutate the caller's state.

    seq is compare-and-swap per task. IDs are globally unique in this journal.
    Identical repeats are no-ops; reusing an ID with different bytes conflicts.
    at must come from the authenticated journal timestamp, not a worker clock.
    """
    if not isinstance(record, dict):
        raise Conflict("record must be an object")
    common = {"id", "project", "task", "seq", "actor", "action", "at"}
    required = {
        "open": {"deadline", "source"},
        "offer": {"to", "accept_by", "next_deadline", "evidence"},
        "accept": {"offer_id", "evidence"},
        "update": {"deadline", "evidence"},
        "wait_user": {"evidence"},
        "resume": {"deadline", "evidence"},
        "finish": {"result", "user_update"},
        "cancel": {"evidence"},
    }
    action = record.get("action")
    if not isinstance(action, str) or action not in required:
        raise Conflict("unknown action")
    if set(record) != common | required[action]:
        raise Conflict("unexpected or missing field")
    rid = identifier(record["id"])
    task_id = identifier(record["task"])
    if record["project"] != state["project"]:
        raise Conflict("wrong project")
    if (not isinstance(record["actor"], str) or record["actor"] not in ACTORS
            or type(record["seq"]) is not int):
        raise Conflict("invalid actor or sequence")
    at = timestamp(record["at"])
    fingerprint = digest(record)
    if rid in state["seen"]:
        if state["seen"][rid] != fingerprint:
            raise Conflict("event ID reused")
        return copy.deepcopy(state)
    new = copy.deepcopy(state)
    task = new["tasks"].get(task_id)
    if action == "open":
        if task is not None or record["seq"] != 1 or record["actor"] != "work":
            raise Conflict("only Work can open a new task")
        task = {"owner": "work", "phase": "active", "offer": None,
                "source": evidence(record["source"])}
        new["tasks"][task_id] = task
    else:
        if task is None or record["seq"] != task["seq"] + 1:
            raise Conflict("unknown task or stale sequence")
        if at < task["at"] or task["phase"] in {"finished", "cancelled"}:
            raise Conflict("old event or closed task")
        if action == "accept":
            offer = task["offer"]
            if (offer is None or record["actor"] != offer["to"]
                    or record["offer_id"] != offer["id"]):
                raise Conflict("acceptance does not match pending handover")
        elif record["actor"] != task["owner"] and not (
            record["actor"] == "work" and (
                action == "cancel" or
                (action == "resume" and task["phase"] == "waiting_user")
            )
        ):
            raise Conflict("sender does not own the baton")
    if action in {"open", "update", "resume"}:
        deadline = timestamp(record["deadline"])
        if deadline <= at:
            raise Conflict("deadline must be in the future")
        if action == "update" and (task["phase"] != "active" or task["offer"]):
            raise Conflict("cannot extend a pending handover or user wait")
        if action == "resume":
            if task["phase"] != "waiting_user" or record["actor"] != "work":
                raise Conflict("Work must record the user's response")
            task["owner"] = "work"
        task.update(deadline=deadline, phase="active")
    elif action == "offer":
        if (task["phase"] != "active" or task["offer"] is not None
                or not isinstance(record["to"], str)
                or record["to"] not in {"work", "hermes"}
                or record["to"] == task["owner"]):
            raise Conflict("invalid handover")
        accept_by = timestamp(record["accept_by"])
        next_deadline = timestamp(record["next_deadline"])
        if not at < accept_by < next_deadline:
            raise Conflict("invalid handover deadlines")
        task["offer"] = {"id": rid, "to": record["to"],
                         "accept_by": accept_by, "next_deadline": next_deadline}
        # Sender still owns the baton. Existing owner deadline is not extended.
    elif action == "accept":
        offer = task["offer"]
        task.update(owner=offer["to"], deadline=offer["next_deadline"], offer=None)
        # Late acceptance is recorded honestly; an expired work deadline stays due.
    elif action == "wait_user":
        if task["owner"] != "work" or task["offer"] is not None:
            raise Conflict("only Work can explicitly ask the user and wait")
        task.update(owner="user", phase="waiting_user", deadline=None)
    elif action == "finish":
        if task["owner"] != "work" or task["offer"] is not None:
            raise Conflict("Work must review and report before closing")
        evidence(record["result"])
        evidence(record["user_update"])
        task.update(phase="finished", deadline=None)
    elif action == "cancel":
        if record["actor"] != "work":
            raise Conflict("Work must record cancellation authority")
        task.update(phase="cancelled", deadline=None, offer=None)
    if "evidence" in record:
        evidence(record["evidence"])
    task.update(seq=record["seq"], at=at, last_record=rid)
    new["seen"][rid] = fingerprint
    return new


def due(state, now):
    """Metadata-only incidents; no claim that an overdue actor crashed."""
    timestamp(now)
    incidents = []
    for task_id, task in sorted(state["tasks"].items()):
        if task["phase"] != "active":
            continue
        kind = None
        if now >= task["deadline"]:
            kind = "update-overdue"
        offer = task["offer"]
        if offer and now >= offer["accept_by"]:
            kind = "handover-unaccepted"
        if kind:
            item = {"project": state["project"], "task": task_id,
                    "owner": task["owner"], "kind": kind,
                    "seq": task["seq"]}
            item["id"] = digest(item)
            incidents.append(item)
    return incidents
