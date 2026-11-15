# PRIVATE release-preparation checklist (not approval to publish)

- Review the proposed diff, every tracked file and complete Git history, including deleted files, commits, author metadata, tags, branches and prior artifacts; review issue/PR titles, bodies, comments, attachments and links for private identifiers, host paths, credentials, household/device information and secret-like data. A clean worktree or credential grep is not sufficient. Resolve sensitive history/metadata without exposing it in a public fork or rewrite without separate approval.
- Confirm examples, source bundle, documentation and portable skill contain only fictional identities and no personal skill icon, host snapshots, private evidence links, credentials or real routes. Run secret scanning appropriate to the eventual publication; manually review context and third-party content. A scanner pass is not a privacy guarantee.
- The owner selected MIT; preserve LICENSE in every source export. Review third-party attribution separately. Establish tunnel binary/client and Work webhook distribution, access and redistribution terms; do not vendor unknown binaries or advertise an unavailable installer.
- Qualify on a genuinely fresh host with independently obtained dependencies, actual `gh` authentication, MCP registration and permitted Work webhook capabilities; validate full callback path and rollback under controlled authority. Offline tests and one existing host are not fresh-host evidence.
- Reconcile source-bundle contents and hashes against the exact reviewed commit; inspect private release channel and recipients. Separately approve visibility, tag/release, merge and any live changes. This checklist does not perform any of them.

## Preparing a separate public edition

Keep this development repository, its history and its operational issues/PRs
private. Prepare a new repository from a reviewed, explicitly selected source
snapshot with a new root commit. Do not push existing branches, tags, remotes,
issue exports or attachments into it. The private doorbell routes remain here;
a public source repository is not an operational notification destination.

Before creating the public edition:

1. The proposed public name is `hermes-connection-kit`; the owner selected the
   MIT licence. Check third-party attribution and preserve LICENSE. A private
   preparation branch is not a public release.
2. Review every proposed source file, example, document and portable skill.
   Replace maintainer-specific paths, routes, account/model choices and private
   evidence references with portable instructions or clearly dated compatibility
   evidence. Exclude maintainer-specific live qualification helpers; retain
   portable isolated tests and document their limits.
3. Review the source-bundle allowlist and its tests for the exact intended
   contents. It includes recovery, portable tests and CI, and excludes the
   maintainer-specific live helper and its dependent tests. Keep the MIT
   licence in the allowlist when exporting. Never bypass unexpected-file refusal
   by indiscriminately archiving the repository.
4. Run the exported source's complete offline tests in a clean directory,
   validate internal links, scan and manually inspect the proposed archive,
   and record the reviewed source revision and per-file hashes.
5. Verify what another account can actually obtain: tunnel runtime and use
   permissions, MCP registration, and (optionally) Work webhook delivery.
   Distinguish confirmed prerequisites from maintainer-only access.
6. Run the guided README with a second operator. Record where they needed
   undocumented help. Confirm one complete harmless round trip and routine
   disconnect without exposing keys.
7. Review the prepared public contents before changing visibility/publishing.
   Add a concise description, relevant topics and a tagged release with source
   hashes and tested-version notes. Then share the public URL with relevant
   communities. No public repository or release is created by this document.

The README's guided setup is the entry point for users and LLM assistants.
Keep technical details in the linked guides; update both when CLI behaviour or
external capabilities change. Cloning the source does not install the portable
skill in the user's assistant.

## Prepared launch packet

Target repository name: `hermes-connection-kit`. Create it from a clean source
snapshot with new history; do not change the visibility of the development repo.
Keep the public source repository separate from private operational ledgers.

GitHub description:

> Connect ChatGPT Work to a self-hosted Hermes Agent, with MCP tools, job tracking and GitHub notifications. Includes guided setup and a portable skill template.

Suggested topics: `hermes-agent`, `mcp`, `chatgpt`, `self-hosted`, `automation`,
`github-webhooks`, `python`, `systemd`.

### Proposed initial release: v0.1.0-preview

An MIT-licensed connection and notification kit for people running Hermes Agent
on Linux. Includes manual connection lifecycle commands, optional GitHub task
notifications, an optional recovery reminder companion, fictional configuration
examples, offline tests and an LLM-guided setup path.

The tested integration uses ChatGPT Work and OpenAI's tunnel runtime. These are
external prerequisites, not bundled services or universally available account
features. Other assistants can reuse the instructions and adapt the tracking
protocol; other client integrations are not qualified. No notification latency
or exactly-once side-effect guarantee is offered. A second-operator walkthrough
and independent account-access check remain outstanding for this preview.

Record the final public commit and archive hashes when cutting the release.
Do not copy a private development commit's history or use its SHA as if it were
the public root commit. External dependencies retain their own terms.

### Community announcement draft — not posted

> I've packaged a workflow for connecting ChatGPT Work to a self-hosted Hermes
> Agent. Work can dispatch an authorised task, and an optional GitHub “doorbell”
> brings the result back to its conversation for verification and acknowledgement.
>
> The kit includes on-demand connect/disconnect, job tracking, guided setup for
> an LLM to walk you through one step at a time, a reusable skill template and
> isolated tests. It's MIT licensed.
>
> This is an early preview: the complete integration needs OpenAI tunnel/MCP
> access and Work webhooks. It isn't plug-and-play for every assistant, although
> the guides and tracking components can be adapted. I'd welcome feedback from
> someone trying the documented setup on a fresh Linux host.
>
> [Insert the verified public repository URL after publication.]

Choose the community's designated showcase channel and follow its current
posting rules. Obtain the owner's approval for the final post before sending.
Do not present this as an official OpenAI or Hermes project or claim their
endorsement. Directory submissions must accurately describe an integration kit,
not a generally hosted MCP server.

### Short demo plan

Aim for a 60–90 second recording in a disposable environment:

1. Show the README's purpose and access requirements.
2. Connect a separately configured test tunnel; show readiness without keys.
3. Ask for one authorised harmless fixed-output task; retain the accepted run.
4. Show the real completion notification, verified response and later host
   acknowledgement reconciliation for that same run.
5. Disconnect and explain that accepted jobs are not cancelled by disconnect.

Use private test routes and redact account identifiers, keys, unrelated tabs and
host information. Do not fabricate a callback or record old results as a new run.
If edited for time, label the cut. Recording requires a separately agreed live
test session; the prepared outline is not a completed demo or authorisation to
restart a stopped host.
