"""Offline illustration of current watchdog body/hash admission, not a webhook dispatcher.

Caller must independently verify GitHub author/route, source authority, tracked registration,
status and full paginated handled ledger before invoking this pure function.
"""
import hashlib
import json
import re

KINDS = frozenset(('completion', 'failed', 'waiting-for-approval', 'overdue', 'connection-blocked'))
MARKER = re.compile(r'<!-- hermes-watchdog:event:([0-9a-f]{64}) -->')
RUN = re.compile(r'run_[0-9a-f]{32}')
SUFFIX = ('Result: retained privately on Hermes host; fetch the known run ID via the approved status route.\n'
          'Delivery is not Work acknowledgement; do not dispatch from this comment.')


def validate_event(body, project, run_id, correlation, max_generation, handled_ids,
                   expected_generation=None):
    """Return (kind, generation, event_id), or refuse; never derive generation from comment order."""
    if not isinstance(body, str) or not RUN.fullmatch(run_id) or not isinstance(project, str) or not isinstance(correlation, str):
        raise ValueError('invalid expected binding')
    if type(max_generation) is not int or not 1 <= max_generation <= 1000:
        raise ValueError('invalid reviewed bound')
    if expected_generation is not None and (type(expected_generation) is not int or not 1 <= expected_generation <= max_generation):
        raise ValueError('invalid expected generation')
    lines = body.split('\n', 5)
    match = MARKER.fullmatch(lines[0]) if lines else None
    if len(lines) != 6 or match is None:
        raise ValueError('invalid marker/body')
    event_id = match.group(1)
    kind = lines[1].removeprefix('Hermes watchdog: ')
    if (kind not in KINDS or lines[1] != 'Hermes watchdog: ' + kind
            or lines[2] != 'Project: ' + project or lines[3] != 'Run: ' + run_id
            or lines[4] != 'Correlation: ' + correlation or lines[5] != SUFFIX):
        raise ValueError('invalid current-format event fields')
    if event_id in handled_ids:
        raise ValueError('already handled')
    candidates = (range(1, max_generation + 1) if expected_generation is None
                  else (expected_generation,))
    matches = [generation for generation in candidates if hashlib.sha256(
        json.dumps([project, run_id, kind, generation], separators=(',', ':')).encode('utf-8')
    ).hexdigest() == event_id]
    if len(matches) != 1:
        raise ValueError('no unique generation in reviewed bound')
    return kind, matches[0], event_id
