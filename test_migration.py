"""Disposable, no-systemd migration tests; no real service or credential reads."""
import importlib.util
import fcntl
import json
import multiprocessing
from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('migration', Path(__file__).with_name('migration.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.old = root / 'old'
        self.old.write_bytes(b'previous')
        self.source = root / 'source'
        self.source.write_bytes(b'candidate')
        self.cred = [root / 'runtime', root / 'gateway']
        for p in self.cred:
            p.write_bytes(b'')
        for p in [self.old, self.source, *self.cred]:
            p.chmod(0o600)
        self.destination = root / 'new'
        self.backup = root / 'backup'
        self.lock = root / 'operation.lock'
        self.lock.write_bytes(b'')
        self.lock.chmod(0o600)
        self.manifest = root / 'manifest.json'
        self.data = {'unit': 'test.service', 'listener_port': 18765,
                     'control_port': 18766, 'lock_path': str(self.lock),
                     'observer_units': ['observer.timer', 'observer.service', 'control.service'],
                     'credentials': list(map(str, self.cred)), 'immutable': [],
                     'directories': [],
                     'files': [{'path': str(self.old), 'old_sha256': m.digest(b'previous'),
                                'source': str(self.source), 'new_sha256': m.digest(b'candidate')},
                               {'path': str(self.destination), 'old_sha256': None,
                                'source': str(self.source), 'new_sha256': m.digest(b'candidate')}]}
        self.save()
        self.guard = patch.object(m, 'stopped', return_value=None)
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def save(self):
        self.manifest.write_text(json.dumps(self.data))
        self.manifest.chmod(0o600)

    def test_dry_run_apply_rollback_preserves_credentials(self):
        m.run(str(self.manifest), 'dry-run', None)
        self.assertEqual(self.old.read_bytes(), b'previous')
        m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertEqual(self.old.read_bytes(), b'candidate')
        self.assertEqual(self.destination.read_bytes(), b'candidate')
        m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(self.old.read_bytes(), b'previous')
        self.assertFalse(self.destination.exists())
        self.assertEqual([p.read_bytes() for p in self.cred], [b'', b''])

    def test_old_mismatch_refuses_without_backup(self):
        self.old.write_bytes(b'foreign')
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertFalse(self.backup.exists())

    def test_source_mismatch_refuses(self):
        self.source.write_bytes(b'foreign')
        with self.assertRaisesRegex(RuntimeError, 'source changed'):
            m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertFalse(self.backup.exists())

    def test_active_state_refuses_before_any_write(self):
        with patch.object(m, 'stopped', side_effect=RuntimeError('managed tunnel is active')):
            with self.assertRaisesRegex(RuntimeError, 'active'):
                m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertFalse(self.backup.exists())

    def test_rollback_refuses_changed_candidate(self):
        m.run(str(self.manifest), 'apply', str(self.backup))
        self.destination.write_bytes(b'foreign')
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(self.old.read_bytes(), b'candidate')

    def test_directory_mode_and_new_directory_roundtrip(self):
        root = Path(self.tmp.name)
        existing = root / 'existing'
        existing.mkdir(mode=0o775)
        existing.chmod(0o775)
        created = root / 'created'
        self.data['directories'] = [
            {'path': str(existing), 'old_mode': 0o775, 'new_mode': 0o700},
            {'path': str(created), 'old_mode': None, 'new_mode': 0o700}]
        self.save()
        m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertEqual(existing.stat().st_mode & 0o777, 0o700)
        self.assertTrue(created.is_dir())
        m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(existing.stat().st_mode & 0o777, 0o775)
        self.assertFalse(created.exists())

    def test_immutable_pin_refuses_changed_binary(self):
        self.data['immutable'] = [{'path': str(self.source), 'sha256': m.digest(b'candidate')}]
        self.save()
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'immutable hash'):
            m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertFalse(self.backup.exists())

    def test_credential_can_never_be_a_source_or_immutable(self):
        self.data['files'][0]['source'] = str(self.cred[0])
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'credential path collision'):
            m.run(str(self.manifest), 'apply', str(self.backup))
        self.data['files'][0]['source'] = str(self.source)
        self.data['immutable'] = [{'path': str(self.cred[1]), 'sha256': m.digest(b'')}]
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'credential path collision'):
            m.run(str(self.manifest), 'apply', str(self.backup))

    def test_stopped_guard_rejects_active_and_listener(self):
        self.guard.stop()
        from unittest.mock import Mock
        active = Mock(returncode=0, stdout='LoadState=loaded\nActiveState=active\nMainPID=99\n')
        with self.assertRaisesRegex(RuntimeError, 'active'):
            m.stopped(self.data, runner=lambda *a, **kw: active)
        inactive = Mock(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\n')
        with patch.object(Path, 'read_text', return_value='sl local_address rem_address st\n0: 0100007F:494D 00000000:0000 0A'):
            with self.assertRaisesRegex(RuntimeError, 'listener'):
                m.stopped(self.data, runner=lambda *a, **kw: inactive)

    def test_observer_and_control_guards(self):
        self.guard.stop()
        from unittest.mock import Mock
        def manager(args, **kw):
            active = args[3] == 'observer.timer'
            return Mock(returncode=0, stdout='LoadState=loaded\nActiveState=' + ('active' if active else 'inactive') + '\nMainPID=0\n')
        with self.assertRaisesRegex(RuntimeError, 'observer.timer'):
            m.stopped(self.data, runner=manager)
        def inactive(args, **kw):
            return Mock(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\n')
        with patch.object(Path, 'read_text', return_value='sl local_address rem_address st\n0: 0100007F:494E 00000000:0000 0A'):
            with self.assertRaisesRegex(RuntimeError, 'listener'):
                m.stopped(self.data, runner=inactive)

    def test_interruption_after_each_file_replacement_recovers_and_retries(self):
        original = m.atomic
        for target in (self.old, self.destination):
            with self.subTest(target=target), tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as scratch:
                # Use the fresh fixture paths; restore their old state between attempts.
                self.old.write_bytes(b'previous')
                if self.destination.exists():
                    self.destination.unlink()
                b = Path(scratch) / 'backup'
                def interrupt(path, data):
                    original(path, data)
                    if Path(path) == target:
                        raise RuntimeError('injected interruption')
                with patch.object(m, 'atomic', side_effect=interrupt):
                    with self.assertRaisesRegex(RuntimeError, 'injected interruption'):
                        m.run(str(self.manifest), 'apply', str(b))
                m.run(str(self.manifest), 'rollback', str(b))
                m.run(str(self.manifest), 'rollback', str(b))
                self.assertEqual(self.old.read_bytes(), b'previous')
                self.assertFalse(self.destination.exists())

    def test_interrupted_rollback_resumes_without_foreign_overwrite(self):
        m.run(str(self.manifest), 'apply', str(self.backup))
        original = m.atomic
        def interrupt(path, data):
            original(path, data)
            if Path(path) == self.old:
                raise RuntimeError('rollback interruption')
        with patch.object(m, 'atomic', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'rollback interruption'):
                m.run(str(self.manifest), 'rollback', str(self.backup))
        self.destination.write_bytes(b'foreign')
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(self.destination.read_bytes(), b'foreign')
        self.destination.write_bytes(b'candidate')
        m.run(str(self.manifest), 'rollback', str(self.backup))

    def test_directory_and_deletion_interruption_recovers(self):
        root = Path(self.tmp.name)
        existing = root / 'existing'
        existing.mkdir()
        existing.chmod(0o775)
        self.data['directories'] = [{'path': str(existing), 'old_mode': 0o775, 'new_mode': 0o700}]
        self.data['files'][0]['source'] = None
        self.data['files'][0]['new_sha256'] = None
        self.save()
        original = m.atomic
        def interrupt(path, data):
            original(path, data)
            if Path(path) == self.destination:
                raise RuntimeError('interrupted after deletion and replacement')
        with patch.object(m, 'atomic', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertFalse(self.old.exists())
        self.assertEqual(existing.stat().st_mode & 0o777, 0o700)
        m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(self.old.read_bytes(), b'previous')
        self.assertEqual(existing.stat().st_mode & 0o777, 0o775)

    def test_incomplete_backup_refuses_rollback_without_mutation(self):
        original = m.atomic
        def interrupt(path, data):
            if Path(path).name == 'transaction.json':
                raise RuntimeError('before journal')
            original(path, data)
        with patch.object(m, 'atomic', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'before journal'):
                m.run(str(self.manifest), 'apply', str(self.backup))
        with self.assertRaises((FileNotFoundError, RuntimeError)):
            m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(self.old.read_bytes(), b'previous')
        self.assertFalse(self.destination.exists())

    def test_manifest_binding_and_lock_inode(self):
        inode = self.lock.stat().st_ino
        m.run(str(self.manifest), 'apply', str(self.backup))
        self.data['control_port'] = 18767
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'transaction manifest mismatch'):
            m.run(str(self.manifest), 'rollback', str(self.backup))
        self.data['control_port'] = 18766
        self.save()
        m.run(str(self.manifest), 'rollback', str(self.backup))
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_multiprocess_lifecycle_lock_refuses_before_stopped(self):
        ctx = multiprocessing.get_context('fork')
        ready, release = ctx.Pipe()
        def holder(path, pipe):
            with open(path, 'rb+') as fd:
                fcntl.flock(fd, fcntl.LOCK_EX)
                pipe.send('locked')
                pipe.recv()
        proc = ctx.Process(target=holder, args=(self.lock, release))
        proc.start()
        self.addCleanup(lambda: proc.join(2))
        try:
            self.assertEqual(ready.recv(), 'locked')
            with patch.object(m, 'stopped', side_effect=AssertionError('guard reached')):
                with self.assertRaisesRegex(RuntimeError, 'lock busy'):
                    m.run(str(self.manifest), 'apply', str(self.backup))
        finally:
            ready.send('release')
            proc.join(2)
        self.assertEqual(proc.exitcode, 0)
        self.assertFalse(self.backup.exists())

    def test_competing_migration_during_mutation_refuses(self):
        ctx = multiprocessing.get_context('fork')
        parent, child = ctx.Pipe()
        other_backup = Path(self.tmp.name) / 'competing-backup'
        def contender(pipe):
            try:
                m.run(str(self.manifest), 'apply', str(other_backup))
                pipe.send('unexpected success')
            except RuntimeError as exc:
                pipe.send(str(exc))
        original = m.atomic
        def contend(path, data):
            original(path, data)
            if Path(path) == self.old:
                proc = ctx.Process(target=contender, args=(child,))
                proc.start()
                try:
                    self.assertTrue(parent.poll(3))
                    self.assertIn('lifecycle lock busy', parent.recv())
                finally:
                    proc.join(3)
                self.assertEqual(proc.exitcode, 0)
        with patch.object(m, 'atomic', side_effect=contend):
            m.run(str(self.manifest), 'apply', str(self.backup))
        self.assertFalse(other_backup.exists())
        m.run(str(self.manifest), 'rollback', str(self.backup))


if __name__ == '__main__':
    unittest.main()
