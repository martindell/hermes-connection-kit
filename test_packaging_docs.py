"""Isolated copied-skill link and documented fresh-host preparation checks."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parent
SKILL = ROOT / 'templates/skills/hermes-collaboration'


class PackagingDocsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_copied_skill_links_remain_inside_and_resolve(self):
        copied = self.base / 'separate-install' / 'hermes-collaboration'
        copied.parent.mkdir()
        shutil.copytree(SKILL, copied)
        skill = (copied / 'SKILL.md').read_text()
        self.assertTrue(skill.startswith('---\nname: hermes-collaboration\ndescription: '))
        self.assertIn('$hermes-collaboration', (copied / 'agents/openai.yaml').read_text())
        found = 0
        for doc in copied.rglob('*.md'):
            for target in re.findall(r'\[[^]]+\]\(([^)]+)\)', doc.read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                resolved = (doc.parent / target.split('#')[0]).resolve()
                self.assertTrue(resolved.is_relative_to(copied.resolve()), target)
                self.assertTrue(resolved.is_file(), target)
                found += 1
        self.assertGreaterEqual(found, 6)
        self.assertIn('maximum generation', (copied / 'references/work-webhook.md').read_text())

    def test_documented_paths_and_settings_parse_without_systemd(self):
        doc = (ROOT / 'docs/fresh-host.md').read_text()
        script = doc.split('```sh\n', 1)[1].split('\n```', 1)[0]
        home = self.base / 'fictional-home'
        home.mkdir(mode=0o700)
        environment = {**os.environ, 'HOME': str(home)}
        def prepare():
            return subprocess.run(['sh', '-c', script], env=environment, capture_output=True, text=True)
        result = prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(prepare().returncode, 0)
        for path in ('.config/systemd/user', '.config/hermes-mcp-connection', '.local/share/hermes-mcp-source'):
            self.assertEqual((home / path).stat().st_mode & 0o777, 0o700)
        self.assertTrue((home / '.local/lib').is_dir())
        self.assertTrue((home / '.local/state').is_dir())
        settings = home / '.config/hermes-mcp-connection/config.json'
        cfg = json.loads((ROOT / 'config.example.json').read_text())
        cfg.update(client_path=str(home / 'client'), client_config_path=str(home / 'client.yaml'),
                   install_dir=str(home / '.local/lib/hermes-mcp-connection'),
                   state_dir=str(home / '.local/state/hermes-mcp-connection'))
        settings.write_text(json.dumps(cfg))
        settings.chmod(0o600)
        spec = importlib.util.spec_from_file_location('candidate_on_demand', ROOT / 'on-demand.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.load_local_config(settings)  # actual parser and secure-existing-parent preflight
        self.assertEqual(module.INSTALL_DIR, Path(cfg['install_dir']))
        self.assertEqual(module.ROOT, Path(cfg['state_dir']))
        # An existing nonprivate target is refused without changing its mode.
        unit_dir = home / '.config/systemd/user'
        unit_dir.chmod(0o755)
        refusal = prepare()
        self.assertNotEqual(refusal.returncode, 0)
        self.assertEqual(unit_dir.stat().st_mode & 0o777, 0o755)
        # Existing shared-writable parent is never silently chmodded.
        unit_dir.chmod(0o700)
        parent = home / '.local'
        parent.chmod(0o777)
        self.assertNotEqual(prepare().returncode, 0)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o777)


if __name__ == '__main__':
    unittest.main()
