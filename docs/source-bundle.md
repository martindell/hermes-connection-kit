# Reproducible source snapshot

The public edition starts from reviewed files, not the development repository's
Git history or operational conversations. The canonical explicit file list is
`FILES` in [tools/source_bundle.py](../tools/source_bundle.py). Review that list
and every included file before distribution.

Included: the MIT licence, connection and migration modules, the portable deadline watchdog, its example notification hook, fictional configuration examples,
README, guides and public demo, portable collaboration skill and references, callback templates,
isolated tests, source packager, event validator and offline CI workflow.

Excluded: the maintainer-specific live helper
`qualification/qualify_tracked_busy.py` and its dependent
`test_busy_qualification.py`. These two paths may be absent in an imported clean
snapshot. Any other unexpected tracked file, missing required file, symlink or
submodule causes refusal. The packager never includes `.git`, untracked files,
live credentials, settings or registry contents.

## Build from an exact revision

Run from a checkout whose packager has also been reviewed. Replace the SHA with
the full reviewed commit before running; do not substitute HEAD by habit.

```sh
SHA=REPLACE_WITH_REVIEWED_FULL_COMMIT_SHA
install -d -m 700 "$HOME/mcp-private-tests"
python3 tools/source_bundle.py "$SHA" "$HOME/mcp-private-tests/source-bundle-new"
sha256sum "$HOME/mcp-private-tests/source-bundle-new/source.tar" "$HOME/mcp-private-tests/source-bundle-new/manifest.json"
```

Output must be new and under an owner-only directory. The tool reads Git blobs,
writes a deterministic tar with fixed metadata and a per-file SHA256 manifest,
then checks every archive member against that manifest. Two exports of the same
commit and allowlist must produce identical archive and manifest bytes.

Extract only the verified archive into a new private directory, run the complete
offline suite there using private TMPDIR, and check all relative document links.
Initialise any future public repository with a new root commit from those files;
do not copy `.git`, push development branches/tags or import issue/PR history.
The included CI workflow can run the portable suite in that repository.

## Publication is a separate decision

The approved MIT licence is included in the explicit allowlist. The current
development manifest is preparation, not a public release. Check external dependency access and
redistribution terms separately; no third-party binaries are bundled.

The manifest proves byte identity, not safety, permission to redistribute or
privacy. Review contents and metadata under the [release checklist](release-preparation.md).
Secrets scanning alone cannot detect every sensitive fact. Keep installation
and qualification ledgers private even if the source becomes public.
