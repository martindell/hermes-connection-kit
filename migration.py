#!/usr/bin/python3
"""Stopped-only, hash-bound file migration. No service, credential or job operations.

Manifest schema: unit (name), listener_port (integer), credentials (two paths),
files ([{path, old_sha256: hex|null, source: path|null, new_sha256: hex|null}]),
directories ([{path, old_mode: octal integer|null, new_mode: octal integer}]).
A null old hash requires absence; a null new hash removes a previously backed-up file.
Manifest and sources must be owner-only and protected from other-user writes.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile


def refuse(message):
    raise RuntimeError(message)


def private_file(path):
    p = Path(path)
    if not p.is_absolute():
        refuse('relative path')
    for parent in (p, *p.parents):
        if parent == Path('/'):
            break
        if parent.is_symlink():
            refuse('symlink: ' + str(parent))
        if parent.exists():
            s = parent.lstat()
            if s.st_uid != os.getuid() and s.st_uid != 0:
                refuse('foreign owner: ' + str(parent))
            if s.st_mode & 0o002:
                refuse('world writable: ' + str(parent))
    if not p.is_file() or p.stat().st_uid != os.getuid() or stat.S_IMODE(p.stat().st_mode) != 0o600 or p.stat().st_nlink != 1:
        refuse('not an owner-only regular file: ' + str(p))
    return p.read_bytes()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def current(path):
    p = Path(path)
    if p.is_symlink():
        refuse('symlink collision: ' + str(p))
    if not p.exists():
        return None
    return digest(private_file(p))


def stopped(manifest, runner=subprocess.run):
    names = [manifest['unit'], *manifest['observer_units']]
    if len(names) != len(set(names)) or len(manifest['observer_units']) != 3:
        refuse('invalid managed unit list')
    for name in names:
        if not name.endswith(('.service', '.timer')) or '/' in name or name.startswith('.'):
            refuse('invalid unit')
        r = runner(['systemctl', '--user', 'show', name, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID'], capture_output=True, text=True, timeout=5)
        if r.returncode:
            refuse('cannot determine manager state')
        props = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
        if (props.get('ActiveState') not in ('inactive', 'failed')
                or props.get('LoadState') not in ('loaded', 'not-found')
                or (name.endswith('.service') and props.get('MainPID') != '0')):
            refuse('managed unit active or state unknown: ' + name)
    # /proc is read-only: reject listeners on any address, not only loopback.
    for port in (manifest['listener_port'], manifest['control_port']):
        if type(port) is not int or not 1 <= port <= 65535:
            refuse('invalid port')
        for table in ('/proc/net/tcp', '/proc/net/tcp6'):
            for line in Path(table).read_text().splitlines()[1:]:
                fields = line.split()
                if int(fields[1].split(':')[1], 16) == port and fields[3] == '0A':
                    refuse('listener still active')


@contextmanager
def lifecycle_lock(path):
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        refuse('invalid lifecycle lock')
    # Never create, unlink or replace the existing lifecycle lock inode.
    fd = os.open(p, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
                or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1
                or (s.st_dev, s.st_ino) != (p.stat().st_dev, p.stat().st_ino)):
            refuse('lifecycle lock metadata mismatch')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            refuse('lifecycle lock busy')
        # Catch a lock pathname replaced while waiting for or acquiring the lock.
        if (s.st_dev, s.st_ino) != (p.stat().st_dev, p.stat().st_ino):
            refuse('lifecycle lock inode changed')
        yield
    finally:
        os.close(fd)


def check(manifest, direction):
    files = manifest['files']
    dirs = manifest['directories']
    paths = [f['path'] for f in files] + [d['path'] for d in dirs]
    if len(paths) != len(set(paths)) or any(not Path(p).is_absolute() for p in paths):
        refuse('duplicate or relative destination')
    creds = manifest['credentials']
    sources = [f['source'] for f in files if f['source'] is not None]
    immutable_paths = [i['path'] for i in manifest['immutable']]
    if (len(creds) != 2 or len(set(creds)) != 2
            or any(c in paths or c in sources or c in immutable_paths for c in creds)):
        refuse('credential path collision')
    for c in creds:
        if not Path(c).is_absolute() or Path(c).is_symlink():
            refuse('invalid credential path')
    for d in dirs:
        p = Path(d['path'])
        if p.is_symlink():
            refuse('directory symlink')
        old = d['old_mode'] if direction == 'apply' else d['new_mode']
        if old is None:
            if p.exists():
                refuse('directory collision: ' + str(p))
        elif not p.is_dir() or p.stat().st_uid != os.getuid() or stat.S_IMODE(p.stat().st_mode) != old:
            refuse('directory mode/ownership mismatch: ' + str(p))
    for f in files:
        expected = f['old_sha256'] if direction == 'apply' else f['new_sha256']
        if current(f['path']) != expected:
            refuse('file hash/absence mismatch: ' + f['path'])
        if direction == 'apply' and f['source'] is not None and digest(private_file(f['source'])) != f['new_sha256']:
            refuse('source changed: ' + f['source'])


def atomic(path, data):
    p = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.migration-', dir=p.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, p)
        dirfd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(manifest_path, action, backup):
    raw = private_file(manifest_path)
    m = json.loads(raw)
    if set(m) != {'unit', 'listener_port', 'control_port', 'observer_units', 'lock_path', 'credentials', 'files', 'directories', 'immutable'}:
        refuse('manifest schema mismatch')
    with lifecycle_lock(m['lock_path']):
        _run_locked(m, digest(raw), action, backup)


def _run_locked(m, manifest_hash, action, backup):
    # Reject all credential aliases before opening ANY manifest-listed source.
    cred_paths = {os.path.normpath(c) for c in m['credentials']}
    destinations = {os.path.normpath(f['path']) for f in m['files']}
    destinations.update(os.path.normpath(d['path']) for d in m['directories'])
    if (os.path.normpath(m['lock_path']) in destinations
            or os.path.normpath(m['lock_path']) in cred_paths
            or os.path.normpath(m['lock_path']) in {os.path.normpath(f['source']) for f in m['files'] if f['source'] is not None}):
        refuse('lifecycle lock collision')
    if (cred_paths & {os.path.normpath(i['path']) for i in m['immutable']}
            or cred_paths & {os.path.normpath(f['path']) for f in m['files']}
            or cred_paths & {os.path.normpath(f['source']) for f in m['files'] if f['source'] is not None}):
        refuse('credential path collision')
    # Bind existing client and retained old YAML without changing their bytes.
    for item in m['immutable']:
        p = Path(item['path'])
        if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_uid != os.getuid():
            refuse('immutable path mismatch')
        if digest(p.read_bytes()) != item['sha256']:
            refuse('immutable hash mismatch: ' + str(p))
    # Credentials are strictly metadata-only: never read/copy/hash them.
    for c in m['credentials']:
        p = Path(c)
        if p.is_symlink() or not p.is_file() or p.stat().st_uid != os.getuid() or stat.S_IMODE(p.stat().st_mode) != 0o600 or p.stat().st_nlink != 1:
            refuse('credential metadata mismatch')
    stopped(m)
    if action != 'rollback':
        check(m, 'apply')
    if action == 'dry-run':
        print('DRY RUN: stopped guard and all old hashes, sources, credentials metadata and directory modes match; no writes')
        return
    b = Path(backup)
    if (not b.is_absolute() or b.is_symlink() or b == Path(m['lock_path'])
            or b in [Path(f['path']) for f in m['files']]
            or b in [Path(d['path']) for d in m['directories']]
            or any(b == Path(f['source']) for f in m['files'] if f['source'] is not None)):
        refuse('backup must be an absolute non-symlink distinct path')
    parent = b.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        refuse('backup parent must be owner-only')
    if action == 'apply':
        if b.exists():
            refuse('backup collision')
        b.mkdir(mode=0o700)
        b.chmod(0o700)
        sync_dir(parent)
        old = {}
        for i, f in enumerate(m['files']):
            if f['old_sha256'] is not None:
                data = private_file(f['path'])
                old[str(i)] = f['old_sha256']
                atomic(b / str(i), data)
        atomic(b / 'old.json', json.dumps(old, sort_keys=True).encode())
        # Durable intent follows complete backups and precedes the first mutation.
        atomic(b / 'transaction.json', json.dumps({'manifest_sha256': manifest_hash,
                                                     'version': 1}, sort_keys=True).encode())
        for d in m['directories']:
            p = Path(d['path'])
            if d['old_mode'] is None:
                p.mkdir(mode=d['new_mode'])
                p.chmod(d['new_mode'])
                sync_dir(p.parent)
            elif d['new_mode'] != d['old_mode']:
                p.chmod(d['new_mode'])
                sync_dir(p.parent)
        for f in m['files']:
            p = Path(f['path'])
            if f['source'] is None:
                p.unlink()
                sync_dir(p.parent)
            else:
                atomic(p, private_file(f['source']))
        print('APPLIED FILES ONLY; service manager untouched; backup:', b)
    else:
        if not b.is_dir() or b.stat().st_uid != os.getuid() or stat.S_IMODE(b.stat().st_mode) != 0o700:
            refuse('backup directory mismatch')
        transaction = json.loads(private_file(b / 'transaction.json'))
        if transaction != {'manifest_sha256': manifest_hash, 'version': 1}:
            refuse('backup transaction manifest mismatch')
        old = json.loads(private_file(b / 'old.json'))
        expected_indices = {str(i) for i, f in enumerate(m['files']) if f['old_sha256'] is not None}
        if set(old) != expected_indices:
            refuse('backup index mismatch')
        for i, f in enumerate(m['files']):
            if f['old_sha256'] is not None and (old.get(str(i)) != f['old_sha256'] or digest(private_file(b / str(i))) != f['old_sha256']):
                refuse('backup mismatch')
        # Inspect ALL paths before changing any: partial apply and partial rollback
        # are safe only when every observation is exactly old, new or absent.
        for f in m['files']:
            if current(f['path']) not in (f['old_sha256'], f['new_sha256']):
                refuse('file hash/absence mismatch: ' + f['path'])
        for d in m['directories']:
            p = Path(d['path'])
            if p.is_symlink() or (p.exists() and (not p.is_dir() or p.stat().st_uid != os.getuid())):
                refuse('directory ownership mismatch: ' + str(p))
            mode = stat.S_IMODE(p.stat().st_mode) if p.exists() else None
            if mode not in (d['old_mode'], d['new_mode']):
                refuse('directory mode mismatch: ' + str(p))
        for i, f in enumerate(m['files']):
            p = Path(f['path'])
            if f['old_sha256'] is None:
                if p.exists():
                    p.unlink()
                    sync_dir(p.parent)
            elif current(p) != f['old_sha256']:
                atomic(p, private_file(b / str(i)))
        for d in reversed(m['directories']):
            p = Path(d['path'])
            if d['old_mode'] is None:
                if p.exists():
                    p.rmdir()
                    sync_dir(p.parent)
            elif stat.S_IMODE(p.stat().st_mode) != d['old_mode']:
                p.chmod(d['old_mode'])
                sync_dir(p.parent)
        print('RESTORED FILES ONLY; credentials/state untouched; backup retained:', b)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('dry-run', 'apply', 'rollback'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--backup', help='new private directory for apply; same directory for rollback')
    args = parser.parse_args()
    if args.action != 'dry-run' and not args.backup:
        parser.error('--backup required')
    run(args.manifest, args.action, args.backup)


if __name__ == '__main__':
    main()
