# Callback recovery qualification

## Scope and observed gap

The 2026-11-15 disposable-host outage test retained a completion notification offline and delivered it after network restoration. Work target discovery then timed out and the callback ended without acknowledging. A user-requested retry verified the existing result and acknowledged it; the host reconciled that acknowledgement. This establishes delivery recovery and assisted callback recovery, not unattended callback recovery.

The observer's `_ack` reads comments and eventually marks missing acknowledgements overdue. It deliberately does not repost delivered events. Keep this behaviour: replaying a completion or changing its generation would misrepresent the underlying run and risks duplicate consumers.

## Proposed first change

Add bounded read retries to the owning Work callback using the exact policy in `templates/work-webhook.md`: maximum three attempts per failed read, four retries per invocation, 180-second admission budget, 10/30-second delays. Retain every authority, hash, route, registration, outcome and duplicate gate. No host code, credentials, job operations or event format change is needed. Repository edits alone do not update installed subscriptions.

## Review scenarios

- Discovery times out once, then succeeds: retry only discovery; verify known run and tracking; one exact ack and one handled receipt.
- Status or tracking times out after discovery: retry only that read; never start/register again.
- Timeout persists: exhaust finite limits, report blocker, leave missing ack missing and subscription enabled.
- Permission/404/mismatch/unexpected output: no transient retry or automatic configuration repair.
- Another consumer acknowledges during backoff: full comment/ledger reread finds it; no duplicate ack.
- A POST response is lost: read to reconcile; do not wrap POST in read-retry logic.
- Connector requests a delay longer than the remaining budget: stop with a precise blocker.
- Invocation exits or crashes: do not claim subsequent automatic recovery. Durable redelivery/backstop remains a separate design gap.

These are acceptance scenarios, not a fully executed test matrix. The bounded opaque-read recovery case has since passed the live A2 qualification below; the other scenarios remain review cases.

## Disposable live qualification plan

First independently review the prompt diff. Preserve a copy of the existing test callback prompt and exact triggers; apply only the retry addendum in its existing owning conversation, keeping all prior run scopes and handled IDs. No shared production callback migration. Rollback restores that prior prompt while preserving triggers and enabled state.

Use a separately recorded, explicitly authorised harmless qualification run and exact source/receipt/registration. Arrange a bounded test-only read failure without editing shared credentials or disrupting other jobs. Capture an actual initial eligible read failure and later successful read within the same callback invocation, all attempt timestamps, exact event/ack IDs, no duplicate comments and subsequent host ack readback. No manual invocation, foreground ack, event repost or job replay can count as an unattended pass. If no transient failure occurs, the test is inconclusive for retry recovery.

Do not launch a job or alter the VM merely by reading this plan. The current outage run is already acknowledged and must not be reused to manufacture a new wake-up. Persistent subscriptions stay enabled. Longer failures and lost wake-ups require a separately reviewed durable recovery protocol; this change does not solve them.

## Opaque connector read errors

A controlled pause of the disposable control service produced `INVALID_ARGUMENT` with exact text `label hermes_tracked: template request failed`; the same known registration was readable after automatic resume. The wrapper hides the underlying failure type. The revised policy permits bounded re-reads for only this exact error shape on locally schema-validated, bodyless status/tracked GET calls for the event's accepted ID. It does not call the failure a proven timeout or retry arbitrary INVALID_ARGUMENT errors. All existing attempt/time budgets and verification gates still apply.

Review cases: exact tracked/status error with matching label and valid known-ID GET is eligible; mismatched label, unknown run, invalid arguments, other error text, discovery and write targets are ineligible. A later explicit authentication/404/schema/route/output error ends retries. Concurrent acknowledgement still requires full comment reconciliation. These are policy review cases, not automated execution tests. The foreground pause/recovery observation is not a webhook retry qualification.

## Live results — 15 November 2026

The policy was activated only on the existing disposable-VM callback. No original-host migration or executable-code change was made.

| Attempt | Observation | Disposition |
| --- | --- | --- |
| A1 | The first tracked read waited about 19 seconds and succeeded when control resumed; zero failed reads or retries. | Retry test inconclusive; delivery and acknowledgement passed. |
| A2 | First tracked read failed with the exact eligible opaque error; one bounded retry succeeded in the same webhook invocation. | Within-invocation read recovery passed, including host acknowledgement reconciliation. |

A2 used a test-specific 5-second first tracked-read timeout and up to 60 seconds for subsequent retries, bounded by the unchanged 180-second recovery budget. Those timeout settings are not a blanket production default. The tool call took longer than its configured upstream timeout; tool/provider overhead is outside that timeout.

A2 used one authorised fixed-output run on a dedicated disposable route.

- First tracked call: 02:52:42.936–02:52:56.772 UTC, exact opaque error `label hermes_tracked: template request failed`, code `INVALID_ARGUMENT`. Its underlying cause remains unknown.
- Explicit 10.002-second wait; retry: 02:53:21.967–02:53:25.706 UTC, HTTP200 and exact registered binding. Recovery took 28.934 seconds from the first failure; one retry call.
- Operator readback reported one delivered event and its exact acknowledgement reconciled.
- Independent automatic control-service recovery reported Result=success and ExecMainStatus=0. No job replay, foreground acknowledgement, manual automation invocation or synthetic event.

Detailed operational receipts are retained in the private development ledger.

## Recovery after the callback exits

Within-invocation retries cannot run after an invocation exits. The optional
[recovery companion](recovery-reminder.md) now provides one distinct, durable,
bounded reminder for a delivered original event. It requires separate activation
and the [recovery callback addendum](../templates/work-recovery.md). It is not
enabled merely by installing this kit or updating an existing callback.

Without that companion, recovery remains an explicit user request in the owning
conversation to verify the existing event. Both paths reuse the accepted run,
verify the original event, reconcile comments and acknowledge the original.
Neither restarts work or replays a completion. See the companion document for
live qualification, process-crash simulations and remaining limitations.
