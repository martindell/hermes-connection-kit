# Qualification boundaries

Offline: run the complete `unittest discover` command in [README](../README.md) with private `TMPDIR`; check config JSON parsing, exact YAML target/schema compatibility against your actual external client, skill frontmatter and relative links. Unit tests use fake managers, fictional keys and disposable loopback; they do not validate the live gateway, Work subscription, actual service-manager or external tunnel distribution.

After separately authorised activation, use exactly one harmless, no-tools, fixed-output task in the intended project and conversation (for example, output the prescribed token only). Record one source instruction and a reviewed first-completion expected generation of 1 in the substantive issue, call the discovered `hermes_start` schema once, retain the unambiguous accepted `run_<32 lowercase hex>` and correlation, then register that ID once with exact project/repo/PR and finite UTC deadline (at least ten minutes ahead for this baseline). Read back `hermes_tracked` and known-run `hermes_status`; an ambiguous response is UNKNOWN, never permission to replay. Allow the real timer and webhook to deliver naturally. Verify terminal output equals the prescribed token, observer event ID/hash for generation 1 (the current body and tracked response omit generation) and exact PR/actor, correct owning Work conversation, marker-only acknowledgement, durable issue handled record, and *later* observer acknowledgement reconciliation. If first-kind generation 1 does not match, refuse and investigate rather than guess. For later events use the manifest's pre-reviewed bounded candidate policy, not comment count; see [webhook template](../templates/work-webhook.md) and fictional offline [validator tests](../test_event_validation.py) for second-generation, out-of-order, missing, wrong-hash and beyond-bound scenarios. A GitHub ack is not proof that the observer saw it. Do not post a foreground event or acknowledgement to simulate delivery. Keep the persistent subscription enabled for duplicates/no-ops/transient failure.

Optional, separately bounded and approved: test overdue only after a real registered deadline, distinguish overdue from a stall, test duplicate delivery and controlled pause/recovery using test-owned state. Never pause a shared observer, cancel an accepted job or restart a shared tunnel merely to exercise an event handler. If a callback does not arrive, compare accepted start, exact registration, gateway terminal state/output, observer state/outbox, GitHub PR comments and webhook run for *the same IDs*; a previous webhook run is not evidence for this event. Check GitHub auth, route/actor, exact PR issue_comment filter and owning conversation. Reconcile ambiguous posts from all paginated PR comments before any retry; do not blindly replay starts, registrations, comments or automation creation.

Connection is shared across threads: check known accepted jobs and pending callback evidence before disconnect. Disconnect stops notifications for all routes while retaining credentials/state; accepted jobs may continue. There is no one-minute delivery SLA: bounded tick budgets, slow calls, capacity and scheduling can delay observation. Host failure stops notifications altogether; this kit does not supply an external watchdog.

## Read back observer acknowledgement locally

The baseline `hermes_tracked` response exposes registration and observed run
status, but **not event acknowledgement state**. After the real Work callback
has posted its exact marker-only acknowledgement, allow the normal observer
timer to reconcile it. On the test host, read only the expected event:

```bash
python3 - 'REPLACE_WITH_VERIFIED_EVENT_ID' <<'PY'
import json
import sys
from pathlib import Path

settings = Path.home() / '.config/hermes-mcp-connection/config.json'
config = json.loads(settings.read_text())
registry = Path(config['state_dir']) / 'watchdog-state' / 'registry.json'
event_id = sys.argv[1]
if len(event_id) != 64 or any(c not in '0123456789abcdef' for c in event_id):
    raise SystemExit('Expected a lowercase SHA256 event ID')
event = json.loads(registry.read_text())['events'].get(event_id)
if event is None:
    raise SystemExit('Expected event not found; do not recreate the job')
for key in ('run_id', 'delivery', 'comment_id', 'ack'):
    print(f'{key}: {event.get(key)}')
PY
```

The expected result is the accepted run ID, the exact GitHub event comment ID,
`delivery: delivered` and `ack: acknowledged`. Record this as operator-supplied
host evidence when the operator runs it; do not describe it as an independent
MCP read. Do not dump the full registry or modify it to make the test pass.
If it remains pending/overdue, inspect the timer and GitHub access, exact whole
ack body and actor before changing anything. Do not generate another ack or
manually invoke Work to manufacture callback success.

## Dated fresh-host evidence

On 12 November 2026, kit `59b3064d54150ffb32f5477588a21b8988908399`
passed 106 offline tests on an updated Ubuntu Server 24.04.3 disposable VM
(system Python 3.12.3, systemd 255). The live walkthrough verified:

- A single fixed-output model run through a separately configured MCP tunnel.
- Exact registration, natural GitHub event delivery, the owning Work callback,
  its exact acknowledgement, and operator-read host acknowledgement reconciliation.
- SSH logout survival for the started gateway/tunnel.
- Disconnect/reconnect with retained credentials and registration.
- Inactive gateway/tunnel after reboot, followed by successful manual recovery.
- One event and one acknowledgement at the post-reconnect and post-reboot checks.

The no-tools requirement was a task instruction, not a server-enforced sandbox.
Requested provider/model were recorded, but served-runtime fields were empty;
this did not independently prove fallback exclusion or absence of tool calls.
No post-reboot model job was submitted. Snapshot creation was reported, not
restore-tested. Live overdue/pause stress behaviour was not tested on this fresh
VM. This evidence is a successful installation walkthrough, not a universal
compatibility claim or notification-latency guarantee. Repeat qualification
for your actual host, credentials, provider, route and owning conversation.

## Subsequent qualification, 13–15 November 2026

The original 12 November walkthrough above is a dated baseline, not the final
test matrix. Later disposable-host testing added:

- A reported snapshot restore followed by a fresh successful doorbell round trip.
- Missing/wrong gateway credentials refused with HTTP 401; unsafe settings and
  changed pinned bytes refused; repeated connect/disconnect handled.
- A real network outage: one completion remained pending while the interface
  was down, delivered on a later attempt after recovery, and was acknowledged
  and reconciled without a replacement job.
- Recovery reminder delivery and original-event acknowledgement, manual
  suppression while paused across a due time, and delivery after resume.
- Three forced simultaneous watchdog/recovery service rounds with a held
  registry lock, followed by successful completion of both services. A separate
  excerpt showed 15 successful natural cycles per service; those were offset
  and do not independently establish collision handling.
- A live Work callback encountering HTTP 503 registry busy, then succeeding
  after a bounded read retry with exact registration/outcome checks, one
  acknowledgement and later host reconciliation.
- Two real child-process SIGKILL/restart tests using private state and a durable
  local GitHub substitute, covering crashes before and after simulated acceptance.

The merged revision also passed 145 offline tests during the original-host
deployment preflight. Counts describe that revision; always run the current
suite rather than treating a historic count as an acceptance criterion.

See [recovery behaviour and limits](recovery-reminder.md) for the detailed
distinction between live checks and isolated simulations. There is no universal
compatibility, exactly-once side-effect or notification-latency guarantee.
Hosted Work-process crashes, real ambiguous remote GitHub acceptance, strict
scheduling fairness and the failed automatic pause coordinator are not claimed
as qualified. Do not copy maintainer-specific live helper scripts as setup
instructions or replay historical test jobs on a new host.
