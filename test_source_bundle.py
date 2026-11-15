"""Offline bundle privacy, refusal and deterministic archive tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('source_bundle', Path(__file__).parent / 'tools/source_bundle.py')
assert SPEC is not None and SPEC.loader is not None
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir(mode=0o700)
        self.run_git('init', '-q')
        self.run_git('config', 'user.email', 'fictional@example.invalid')
        self.run_git('config', 'user.name', 'Fictional')
        (self.repo / 'safe.txt').write_text('fictional source\n')
        (self.repo / 'private.txt').write_text('not to distribute\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'fixture')
        self.sha = self.run_git('rev-parse', 'HEAD').strip()

    def run_git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def test_contents_privacy_manifest_and_reproducibility(self):
        # Explicit exclusion is reviewed metadata, not a blanket privacy guarantee.
        one = self.base / 'one'
        two = self.base / 'two'
        for out in (one, two):
            manifest = bundle.build(self.repo, self.sha, out, files=('safe.txt',),
                                    excluded=frozenset({'private.txt'}))
            self.assertEqual(list(manifest['files']), ['safe.txt'])
            self.assertEqual(json.loads((out / 'manifest.json').read_text()), manifest)
            with tarfile.open(out / 'source.tar') as tar:
                self.assertEqual(tar.getnames(), ['safe.txt'])
                stream = tar.extractfile('safe.txt')
                self.assertIsNotNone(stream)
                assert stream is not None
                self.assertEqual(stream.read(), b'fictional source\n')
            self.assertEqual((out / 'source.tar').stat().st_mode & 0o777, 0o600)
        self.assertEqual((one / 'source.tar').read_bytes(), (two / 'source.tar').read_bytes())
        self.assertEqual((one / 'manifest.json').read_bytes(), (two / 'manifest.json').read_bytes())

    def test_unexpected_or_missing_path_and_existing_output_refused(self):
        with self.assertRaisesRegex(ValueError, 'unexpected tracked'):
            bundle.build(self.repo, self.sha, self.base / 'bad', files=('safe.txt',))
        with self.assertRaisesRegex(ValueError, 'unexpected tracked'):
            bundle.build(self.repo, self.sha, self.base / 'bad', files=('absent.txt',),
                         excluded=frozenset({'private.txt'}))
        self.assertFalse((self.base / 'bad').exists())
        (self.base / 'exists').mkdir()
        with self.assertRaisesRegex(ValueError, 'must be new'):
            bundle.build(self.repo, self.sha, self.base / 'exists', files=('safe.txt',),
                         excluded=frozenset({'private.txt'}))

    def test_symlink_and_nonprivate_parent_refused(self):
        (self.repo / 'safe.txt').unlink()
        (self.repo / 'safe.txt').symlink_to('private.txt')
        self.run_git('add', '-A')
        self.run_git('commit', '-qm', 'linked')
        linked = self.run_git('rev-parse', 'HEAD').strip()
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            bundle.build(self.repo, linked, self.base / 'linked', files=('safe.txt',),
                         excluded=frozenset({'private.txt'}))
        self.assertFalse((self.base / 'linked').exists())
        public = self.base / 'public'
        public.mkdir(mode=0o700)
        public.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'owner-only'):
            bundle.build(self.repo, self.sha, public / 'out', files=('safe.txt',),
                         excluded=frozenset({'private.txt'}))

    def test_clean_export_can_be_rebundled_with_optional_exclusions_absent(self):
        self.run_git('rm', 'private.txt')
        self.run_git('commit', '-qm', 'clean export')
        sha = self.run_git('rev-parse', 'HEAD').strip()
        manifest = bundle.build(self.repo, sha, self.base / 'clean',
                                files=('safe.txt',), excluded={'private.txt'})
        self.assertEqual(set(manifest['files']), {'safe.txt'})
        with self.assertRaisesRegex(ValueError, 'unexpected tracked'):
            bundle.build(self.repo, sha, self.base / 'overlap',
                         files=('safe.txt',), excluded={'safe.txt'})

    def test_default_manifest_builds_complete_portable_checkout(self):
        source = Path(__file__).parent
        self.run_git('rm', 'safe.txt', 'private.txt')
        for name in bundle.FILES:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / name).read_bytes())
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'portable kit')
        sha = self.run_git('rev-parse', 'HEAD').strip()
        manifest = bundle.build(self.repo, sha, self.base / 'kit')
        self.assertIn('LICENSE', manifest['files'])
        self.assertIn('recovery.py', manifest['files'])
        self.assertIn('.github/workflows/tests.yml', manifest['files'])
        self.assertIn('templates/work-recovery.md', manifest['files'])
        for name in bundle.EXCLUDED:
            self.assertNotIn(name, manifest['files'])
        # The private development tree may contain the exact excluded pair.
        for name in bundle.EXCLUDED:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('private fixture, never export')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'private helper fixtures')
        sha = self.run_git('rev-parse', 'HEAD').strip()
        second = bundle.build(self.repo, sha, self.base / 'development')
        self.assertEqual(second['files'], manifest['files'])

    def test_exact_commit_required(self):
        with self.assertRaisesRegex(ValueError, 'full lowercase'):
            bundle.build(self.repo, 'HEAD', self.base / 'bad')

    def test_reviewed_gitignore_included_but_ignored_private_files_not_bundled(self):
        (self.repo / '.gitignore').write_text('credentials/\nclient-config-real*.yaml\n')
        self.run_git('add', '.gitignore')
        self.run_git('commit', '-qm', 'reviewed ignore')
        sha = self.run_git('rev-parse', 'HEAD').strip()
        (self.repo / 'credentials').mkdir()
        (self.repo / 'credentials' / 'runtime.key').write_text('fictional private fixture')
        (self.repo / 'client-config-real.yaml').write_text('fictional private yaml')
        output = self.base / 'with-ignore'
        manifest = bundle.build(self.repo, sha, output, files=('.gitignore', 'safe.txt'),
                                excluded=frozenset({'private.txt'}))
        self.assertEqual(set(manifest['files']), {'.gitignore', 'safe.txt'})
        with tarfile.open(output / 'source.tar') as tar:
            self.assertEqual(set(tar.getnames()), {'.gitignore', 'safe.txt'})
            stream = tar.extractfile('.gitignore')
            assert stream is not None
            self.assertEqual(stream.read(), b'credentials/\nclient-config-real*.yaml\n')
        self.assertNotIn('fictional private fixture', (output / 'manifest.json').read_text())


if __name__ == '__main__':
    unittest.main()
