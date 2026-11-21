"""Separate-process runner checks with a fake journal and local notifier."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import baton
import baton_runner as runner


class ProcessTests(unittest.TestCase):
    def test_review_deadline_persists_across_processes_and_closes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = dict(repo="example/repo", issue=1, project="test", author="owner")
            session = "a" * 32
            runner.atomic(root / "config.json", config)
            (root / "github-token").write_text("fake-token")
            (root / "github-token").chmod(0o600)
            runner.atomic(root / "alerts.json", {})
            runner.atomic(root / "state.json", dict(version=1,
                binding=baton.digest(config), session=session, core=baton.empty("test"),
                hashes={}, fault=False, fault_generation=0, last_ok=0))
            counter = root / "sent"
            notifier = root / "notify"
            notifier.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n"
                + f"p=Path({str(counter)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n")
            notifier.chmod(0o700)
            journal = root / "journal.json"
            harness = root / "run.py"
            harness.write_text("import sys,json\nfrom pathlib import Path\n"
                + f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r})\n"
                + "import baton_runner as r\n"
                + f"r.GitHub.comments=lambda self:json.loads(Path({str(journal)!r}).read_text())\n"
                + f"r.os.environ['HERMES_BATON_NOTIFY']={str(notifier)!r}\n"
                + "r.main()\n")
            def tick():
                result = subprocess.run([sys.executable, "-I", str(harness),
                    "--directory", str(root), "tick", "--notify"],
                    check=True, capture_output=True, text=True, timeout=5)
                return json.loads(result.stdout)
            base = int(time.time()) - 100
            records = []
            def add(seq, actor, action, **extra):
                at = datetime.datetime.fromtimestamp(base + seq,
                    datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                body = dict(id=f"e{seq}", project="test", task="review", session=session,
                            seq=seq, actor=actor, action=action, **extra)
                records.append(dict(id=seq, user={"login":"owner"},
                    issue_url="https://api.github.com/repos/example/repo/issues/1",
                    created_at=at, updated_at=at, body=runner.MARKER+"\n"+json.dumps(body)))
                journal.write_text(json.dumps(records))
            add(1, "work", "open", deadline=base+50, source="source:1")
            add(2, "work", "offer", to="hermes", accept_by=base+10,
                next_deadline=base+40, evidence="run:accepted")
            add(3, "hermes", "accept", offer_id="e2", evidence="run:accepted")
            add(4, "hermes", "offer", to="work", accept_by=base+15,
                next_deadline=base+30, evidence="result:1")
            add(5, "work", "accept", offer_id="e4", evidence="review:started")
            first = tick()
            self.assertEqual(first["incidents"][0]["owner"], "work")
            self.assertEqual(first["incidents"][0]["kind"], "update-overdue")
            self.assertEqual(tick()["incidents"], first["incidents"])
            self.assertEqual(counter.read_text(), "x")
            journal.write_text("broken")
            self.assertFalse(tick()["read_ok"])
            self.assertFalse(tick()["read_ok"])
            self.assertEqual(counter.read_text(), "xx")
            add(6, "work", "finish", result="review:verified", user_update="chat:reported")
            self.assertEqual(tick()["incidents"], [])
            self.assertEqual(counter.read_text(), "xx")
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["core"]["tasks"]["review"]["phase"], "finished")
            self.assertFalse(state["fault"])


if __name__ == "__main__":
    unittest.main()
