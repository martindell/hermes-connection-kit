# Project manifest and durable issue receipt template

Fill this in the project's substantive private issue; do not paste real credentials, secret URLs or sensitive outputs. Keep the doorbell PR open and separate from task results. This is a template, not a working route.

Project: [PROJECT_KEY]
Repository: [OWNER/REPO] (private)
Substantive issue: [ISSUE_NUMBER]
Exact open doorbell PR: [PR_NUMBER]
GitHub publishing/ack actor: [ACTOR]
Owning Work conversation/subscription: [CONVERSATION / SUBSCRIPTION ID]
Reviewed implementation SHA: [FULL_SHA]
Permitted actions and exclusions: [BOUNDED_SCOPE]
Source authority: [USER_INSTRUCTION_REFERENCE]
Qualification: [FIXED_OUTPUT_AND_CHECKS]
Callback generation policy: for first fixed-output qualification, expect generation 1 for the reviewed kind; for general events, reviewed per-run/kind maximum [MAX_GENERATION] (finite positive integer, <=1000 for the supplied validator). Check every candidate hash in 1..bound, require exactly one match and an unhandled event ID. Record policy before delivery; refuse/report zero or ambiguous matches rather than widening automatically. Body and tracked readback do not contain generation; missing/out-of-order PR comments cannot be used as a counter. GitHub actor, route, source, registration and run status are independent gates.

Example ledger entries (fictional placeholders; write as separate unmarked issue comments):

Source: [SOURCE_COMMENT_ID]; intent [BOUNDED_ACTION]; correlation [CORRELATION]; private worktree [PATH]; return issue [ISSUE_NUMBER]. No automatic extra tasks.
Start receipt: [SOURCE_COMMENT_ID] accepted run_id [RUN_ID], disposition [ACCEPTED]; unknown response means reconcile, not replay.
Registration receipt: [RUN_ID], [PROJECT_KEY], [OWNER/REPO]#[PR_NUMBER], correlation [CORRELATION], absolute Unix UTC deadline_at [INTEGER]; tracked readback [EXACT_FIELDS]. Registration never starts work.
Handled receipt: event [EVENT_ID], event comment [EVENT_COMMENT_ID], ack comment [ACK_COMMENT_ID], verified run outcome [OUTCOME], result review [REVIEW_RESULT], observer acknowledgement reconciliation [PENDING_OR_VERIFIED].

Never add event/ack marker syntax to issue handoffs. Verify all pages for prior source/start/handled records and concurrent changes before writing.
