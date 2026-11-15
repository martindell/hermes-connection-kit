# Opt-in recovery reminder protocol

`recovery.py` is a separate companion to `watchdog.py`. It offers one additional
notification when Work has not acknowledged an original event. It never submits
or controls a job and never changes the original event, generation or registry.
It imports only the adjacent `watchdog.py`, including under Python isolated mode.

## Behaviour

- Activation binds one exact project/repository/PR/actor to a separate owner-only
  state directory. It excludes every run already registered at activation.
  Existing historical events are never adopted. Re-activation refuses to reset
  history; pause/resume preserves it.
- Only delivered events belonging to subsequently registered runs on that exact
  route are enrolled. Enrolment starts a 10-minute grace period and a 24-hour
  lifetime. There is no claim that lack of acknowledgement means Work crashed.
- Each tick checks all paginated comments, the exact actor, immutable source
  binding and full original comment body. An exact original acknowledgement
  suppresses the reminder. Source drift or malformed/duplicate reminders fail
  closed. A failed GET never permits a POST.
- At most one POST per tick and one POST attempt per original event. Durable
  intent precedes POST. A crash or lost response is reconciled by reads only;
  an absent uncertain reminder is never blindly reposted. This intentionally
  sacrifices delivery if the process died after intent but before sending.
- Reminders have a distinct marker and contain metadata only. Their ID is the
  lowercase SHA256 of compact UTF-8 JSON
  `["hermes-watchdog:recovery", original_event_id, original_comment_id, 1]`.
  This `1` is reminder protocol version/sequence, not execution generation.
- Recovery pauses when either its own state or the original observer is paused.
  No state directory, credentials, subscription or scheduler is removed.
  One route GET and at most one POST are admitted per tick. Both state locks are
  held; persistent contention fails closed. The original watchdog can reconcile the original
  acknowledgement as before. Expired/conflicted recovery records are retained.

## Owning callback contract

Use the separately supplied recovery callback addendum only after review. It must
fetch the original comment and verify the original event using every existing
authority, hash, registration, outcome and duplicate gate. It acknowledges the
**original event/comment**, never the reminder. Exact accepted run scopes remain
mandatory; a recovery marker grants no new job or project authority.

A reminder can race an original callback. GitHub does not provide an atomic
"create if absent" comment operation. Full reconciliation immediately before
writing reduces the race but does not guarantee exactly one acknowledgement
across concurrent Work invocations. Duplicate acknowledgements must be detected
and reported; they must not lead to duplicate job dispatch or result acceptance.
This protocol does not claim exactly-once callback side effects.

### Busy registration reads

The observer and recovery companion hold the registry lock during a tick,
including bounded GitHub requests. An authenticated `GET /tracked/<run_id>`
that encounters lock contention returns HTTP 503 with `{"error":"registry busy"}`.
It does not imply that the run is missing. Missing/unavailable registrations
remain HTTP 404; authentication failures remain HTTP 401. Registration POST
semantics are unchanged, and no write is automatically retried.

The owning callback's existing bounded read-retry policy permits HTTP 5xx:
at most three attempts per read, four retry calls per invocation, ten/thirty-second
waits and a 180-second retry admission budget. Every successful read must still
pass the full binding and outcome checks. Do not broaden retries to HTTP 404
or infer contention from an arbitrary error. Persistent contention can exhaust
this budget; a single recovery reminder does not guarantee further wake-ups.

### Coincident timer ticks

Scheduled watchdog and recovery ticks wait up to five seconds for the shared
registry lock, using monotonic time and short sleeps. This handles brief collisions
when both timers fire together without delaying control requests: HTTP reads and
registration writes still acquire the lock without waiting. The watchdog counts
lock waiting against its existing 39-second I/O admission budget; its service
timeout is unchanged. Recovery does not wait for its own state lock while holding
the shared lock. Failure to acquire either required lock permits no tick writes or
network operations. A sustained collision still fails visibly and relies on the
next scheduled tick; there is no unbounded wait, fairness guarantee or write retry.

Recovery still holds both locks through its GitHub requests. Releasing the source
lock would require revalidation of observer pause state, binding and acknowledgement
before posting; that broader concurrency change is intentionally not included.
Install the matching watchdog module beside recovery.py as well as in the original
observer directory: the new companion uses its adjacent module's bounded-wait API.
Do not mix a new recovery.py with an older watchdog.py.

## Installation and rollback boundary

Nothing in this change installs or enables a service. Existing installers and
timers are unchanged, and existing callbacks will ignore the new marker.
Do not run activation against the shared host as part of reading this document.

Before a disposable installation: verify exact source hashes and ownership of
both adjacent Python files, select a new private recovery state directory,
confirm route/actor and callback owner, prepare and review an exact separate
systemd user unit/timer, back up the callback prompt, and install its scoped
addendum before activation. Keep the existing gateway/tunnel/control unchanged.
The companion's private state and both module hashes must be covered by that
installation review; the original lifecycle manager does not manage this module.

The HTTP 503 correction must also be installed in the watchdog module used by
the **control service**. Updating only the companion's adjacent copy does not
change the running control endpoint. Review that separate pinned-file change,
any stored hashes and the exact control-unit restart/rollback before installing;
never restart a shared service from a callback or overwrite an unexpected file.

CLI shape (substitute reviewed paths and exact identifiers):

```text
python3 -I /reviewed/path/recovery.py \
  --watchdog-state /private/existing/watchdog-state \
  --state-dir /private/new/recovery-state \
  --project PROJECT --repo OWNER/REPO --pr NUMBER --actor ACTOR activate
```

Use the same arguments with `tick` for a single cycle; `pause` and `resume`
change only the companion's own enabled flag. An external reviewed host timer
is needed for unattended cycles. This is not a Work polling automation.
Rollback stops only that companion timer/service and pauses its state; retain
state for reconciliation. Restore only the backed-up callback addendum when no
in-flight recovery callback requires it. Never delete/reset state to retry an
uncertain POST. Original observer/service/subscription remain enabled.

## Validation and remaining work

19 offline recovery tests, 38 watchdog tests and six contention tests pass. Fakes model callback
exit, process restart, lost POST response, death before POST, acknowledgement
races, expiry, pause, old-run exclusion, route/hash/actor mismatch, duplicate
reminders, read failure, lock contention and one-POST-per-tick behaviour. They do
not run Work or prove live webhook delivery. Two real loopback HTTP tests exercise
the authenticated control handler with an actual held registry lock: HTTP 503
while busy, HTTP 200 with unchanged registration after release, and HTTP 401/404
for authentication, missing-run and disallowed-route cases. Python isolated-mode
CLI help works. Run the suites with `TMPDIR` set to a private temporary directory
under your home directory: the existing watchdog fixtures deliberately retain
production ancestor checks and reject shared writable `/tmp`.
The contention tests use real flock locks and concurrent threads with separate
lock descriptors. They exercise both timer orders, successful progress after
release, bounded refusal without side effects, unchanged immediate control reads,
lock-wait admission accounting and no duplicate reminder after repeated ticks.

On the disposable VM, the pre-503 revision passed a deliberate callback
exit-before-ack simulation: a later natural reminder triggered verification and
one original-event acknowledgement, both host registries reconciled, and five
further scheduled ticks produced no duplicate. Idle pause/resume and retained-state
rollback passed. A later pending-reminder test verified suppression across its due
time while scheduled ticks continued and one delivery after resume. Its automatic
callback stopped on the old ambiguous HTTP 404 and required an explicit user retry;
that attempt is not an unattended end-to-end pass. The automatic pause coordinator
also failed with an unestablished cause; stopping only the companion before pausing
allowed manual coordination. Do not present that coordinator as qualified.

The HTTP 503 revision was subsequently installed on the disposable VM. A real
held-lock endpoint check passed HTTP 200 -> 503 -> 200 with unchanged registration.
A separate natural recovery reminder triggered an unattended original-event ack
and both host registries reconciled. Neither of those callbacks encountered HTTP 503; the subsequent dedicated test below supplies that evidence.

That run also exposed coincident starts: recovery won four successive short lock
races, then watchdog won and recovery failed. The
five-second tick wait addresses this observed brief-collision pattern. Revision
`d2e9e6a864608e482bb02a87d4ae507895a73acd` subsequently passed 63 tests on the
disposable VM and was installed with private backups. Three forced-overlap rounds
held the real shared lock for one second while both services started: both remained
activating, then both finished successfully after release.
A later journal excerpt contains 15 successful natural cycles from each service,
16:47–17:02 UTC, with no failures in the excerpt. Those natural cycles
were offset; the forced-overlap test supplies the collision evidence. This does not
establish that an earlier interval omitted by the capped journal was failure-free.

Final cleanup paused/stopped the recovery companion while retaining original and
recovery acknowledgements and the reminder. Gateway, tunnel, control and
original watchdog timer were all reported active. A full paginated route PR reread
found one original event, one reminder and one exact original acknowledgement;
no duplicate was observed. The temporary callback scope was removed and the
persistent subscription remains enabled. No new job or notification was replayed
to manufacture these checks; no production services were installed or enabled.

Remaining boundaries: the end-to-end unattended reminder test preceded the small
lock-wait revision; the new revision has offline concurrency, forced service overlap
and natural idle-cycle evidence. A subsequent dedicated live callback test received HTTP 503 and recovered, as recorded below. Persistent lock contention, strict scheduling fairness,
real Work-process crashes and the previously failed automatic pause coordinator
are not claimed as passed. These are documented limits, not universal guarantees.

One reminder is a bounded second chance, not guaranteed delivery. It cannot
recover a powered-off host, indefinite GitHub outage, absent Work subscription,
or repeated callback failure. Longer retry chains would need separate design.

### Subsequent qualification, 15 November 2026

A dedicated disposable-host test held the real registry lock for approximately
45 seconds. The owning Work callback received HTTP 503 with registry-busy JSON,
waited under its existing bounded read-retry policy, then received HTTP 200 for
the same registration after release. It verified the exact terminal output and
posted the original event acknowledgement; the host subsequently reconciled it.
No replacement job or duplicate acknowledgement was needed. This qualifies that
specific live contention/retry path, not unlimited retries or a delivery SLA.

Two additional isolated tests in test_recovery_process_crash.py killed a real
child process with SIGKILL before and after acceptance by a durable local GitHub
substitute. Fresh processes retained intent, reconciled an accepted reminder and
acknowledgement, and never replayed the uncertain POST. Both passed on the
disposable VM. The before-acceptance case deliberately sacrifices delivery and
expires. These are real process/lock/state tests with simulated remote acceptance,
not crashes of hosted Work, live ambiguous GitHub acceptance or systemd restart.

The automatic pause coordinator remains unqualified. Manual pause/resume
evidence does not establish that coordinator's correctness. Source qualification
does not install or activate recovery on another host; each deployment still
needs its own route, callback and scheduler review.
