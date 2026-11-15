#!/usr/bin/env python3
"""Create deterministic, reviewed Git-blob source bundle; never read live files."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    '.gitignore', 'LICENSE', '.github/workflows/tests.yml',
    'on-demand.py', 'watchdog.py', 'migration.py', 'recovery.py',
    'client-config.example.yaml', 'config.example.json', 'watchdog-config.example.json',
    'README.md', 'INTEGRATION.md', 'MIGRATION.md',
    'docs/fresh-host.md', 'docs/qualification.md', 'docs/release-preparation.md',
    'docs/source-bundle.md', 'docs/callback-recovery.md', 'docs/recovery-reminder.md',
    'test_on_demand.py', 'test_watchdog.py', 'test_integration.py',
    'test_migration.py', 'test_source_bundle.py', 'tools/source_bundle.py',
    'test_event_validation.py', 'test_packaging_docs.py', 'tools/event_validation.py',
    'test_recovery.py', 'test_recovery_process_crash.py', 'test_contention.py',
    'templates/work-recovery.md',
    'templates/work-webhook.md', 'templates/project-manifest.md',
    'templates/skills/hermes-collaboration/SKILL.md',
    'templates/skills/hermes-collaboration/agents/openai.yaml',
    'templates/skills/hermes-collaboration/references/control-room-contract.md',
    'templates/skills/hermes-collaboration/references/deployment.md',
    'templates/skills/hermes-collaboration/references/new-project.md',
    'templates/skills/hermes-collaboration/references/receipts.md',
    'templates/skills/hermes-collaboration/references/project-manifest.md',
    'templates/skills/hermes-collaboration/references/work-webhook.md',
)
# Reviewed maintainer-only helper and its dependent tests; absent in clean exports.
EXCLUDED = frozenset(('qualification/qualify_tracked_busy.py', 'test_busy_qualification.py'))


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)


def build(repo, commit, destination, files=FILES, excluded=EXCLUDED):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Pass an exact full lowercase commit SHA')
    if git(repo, 'rev-parse', '--verify', f'{commit}^{{commit}}').strip().decode() != commit:
        raise ValueError('Not the specified commit')
    listing = git(repo, 'ls-tree', '-rz', commit).split(b'\0')
    entries = {}
    for row in filter(None, listing):
        meta, name = row.split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split()
        path = name.decode('utf-8')
        entries[path] = (mode, kind, oid)
    expected = set(files)
    if (len(expected) != len(files) or expected & set(excluded)
            or not expected <= set(entries)
            or set(entries) - expected - set(excluded)):
        raise ValueError('Missing or unexpected tracked path; review the allowlist')
    for path, (mode, kind, _) in entries.items():
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('Symlink/submodule/non-file refused: ' + path)
    dest = Path(destination)
    if dest.is_symlink() or dest.exists():
        raise ValueError('Output directory must be new')
    parent = dest.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise ValueError('Output parent must be an owner-only directory')
    blobs = {}
    for path in sorted(files):
        oid = entries[path][2]
        blobs[path] = git(repo, 'cat-file', 'blob', oid)
    # Materialise only after all Git blobs and path types pass validation.
    dest.mkdir(mode=0o700)
    try:
        manifest = {'commit': commit, 'files': {p: hashlib.sha256(b).hexdigest()
                                               for p, b in blobs.items()}}
        with tarfile.open(dest / 'source.tar', 'w', format=tarfile.USTAR_FORMAT) as tar:
            for path, data in blobs.items():
                member = tarfile.TarInfo(path)
                member.size = len(data)
                member.mode = 0o644
                member.mtime = 0
                member.uid = member.gid = 0
                member.uname = member.gname = ''
                tar.addfile(member, io.BytesIO(data))
        with tarfile.open(dest / 'source.tar', 'r') as tar:
            if tar.getnames() != sorted(files) or any(not m.isfile() for m in tar):
                raise ValueError('Archive member mismatch')
            for path, digest in manifest['files'].items():
                stream = tar.extractfile(path)
                if stream is None or hashlib.sha256(stream.read()).hexdigest() != digest:
                    raise ValueError('Archive hash mismatch: ' + path)
        (dest / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        for p in dest.iterdir():
            p.chmod(0o600)
    except BaseException:
        # Only own newly created output; avoid leaving a plausible partial bundle.
        for p in dest.iterdir():
            p.unlink()
        dest.rmdir()
        raise
    return manifest


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('Usage: source_bundle.py FULL_COMMIT_SHA NEW_OUTPUT_DIRECTORY')
    os.umask(0o077)
    try:
        result = build(ROOT, sys.argv[1], sys.argv[2])
        print('Bundled', len(result['files']), 'reviewed regular blobs from', result['commit'])
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit('Refused: ' + str(exc))
