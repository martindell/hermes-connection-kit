import copy
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

import baton
from baton_notify import NOTIFY, notification_command, submit, validate_receipts


class NotifyTests(unittest.TestCase):
    def setUp(self):
        state = baton.apply(baton.empty("test"), dict(id="e1", project="test",
            task="t1", seq=1, actor="work", action="open", at=10,
            deadline=20, source="test:source"))
        self.incident = baton.due(state, 21)[0]
        self.receipts = {}
        self.saved = []
        self.save = lambda value: self.saved.append(copy.deepcopy(value))

    def test_notification_command_uses_selected_host_interface(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(notification_command("subject", "message"),
                [NOTIFY, "-e", "Unraid Server Notice", "-s", "subject",
                 "-d", "message", "-i", "warning"])
            os.environ["HERMES_BATON_NOTIFY"] = "/opt/my hook"
            self.assertEqual(notification_command("subject", "message"),
                ["/opt/my hook", "subject", "message"])
            os.environ["HERMES_BATON_NOTIFY"] = "relative"
            with self.assertRaises(OSError):
                notification_command("subject", "message")

    def test_persist_before_send_and_suppress_duplicate(self):
        def run(*args, **kwargs):
            self.assertEqual(next(iter(self.saved[-1].values()))["status"], "intent")
            self.assertNotIn("shell", kwargs)
        mock = Mock(side_effect=run)
        self.assertEqual(submit(self.incident, self.receipts, self.save, mock), "submitted")
        submit(self.incident, self.receipts, self.save, mock)
        mock.assert_called_once()

    def test_timeout_is_not_replayed(self):
        mock = Mock(side_effect=subprocess.TimeoutExpired("notify", 15))
        self.assertEqual(submit(self.incident, self.receipts, self.save, mock), "uncertain")
        submit(self.incident, self.receipts, self.save, mock)
        mock.assert_called_once()

    def test_failed_intent_write_prevents_notification(self):
        mock = Mock()
        with self.assertRaises(OSError):
            submit(self.incident, self.receipts, Mock(side_effect=OSError()), mock)
        mock.assert_not_called()
        self.assertEqual(self.receipts, {})

    def test_restart_after_intent_does_not_replay(self):
        self.receipts[self.incident["id"]] = "intent"
        mock = Mock()
        submit(self.incident, self.receipts, self.save, mock)
        mock.assert_not_called()

    def test_metadata_tampering_rejected_before_send(self):
        self.incident["task"] = "changed"
        mock = Mock()
        with self.assertRaises(ValueError):
            submit(self.incident, self.receipts, self.save, mock)
        mock.assert_not_called()

    def test_uncertain_submission_has_bounded_spaced_retries(self):
        mock = Mock(side_effect=subprocess.TimeoutExpired("notify", 15))
        for now in (10, 20, 910, 920, 1810, 1820, 2710):
            submit(self.incident, self.receipts, self.save, mock, now=now)
        self.assertEqual(mock.call_count, 3)

    def test_crash_after_intent_can_retry_after_grace(self):
        self.receipts[self.incident["id"]] = dict(status="intent", attempts=1, next_attempt=900)
        mock = Mock()
        submit(self.incident, self.receipts, self.save, mock, now=899)
        mock.assert_not_called()
        submit(self.incident, self.receipts, self.save, mock, now=900)
        mock.assert_called_once()

    def test_pending_handover_names_receiver_and_retained_owner(self):
        for owner, receiver in (("work", "hermes"), ("hermes", "work")):
            incident = dict(project="test", task="t1", owner=owner,
                            kind="handover-unaccepted", seq=2)
            incident["id"] = baton.digest(incident)
            mock = Mock()
            submit(incident, {}, self.save, mock, now=100)
            args = mock.call_args.args[0]
            message = args[args.index("-d") + 1]
            self.assertIn(f"waiting for {receiver} to accept", message)
            self.assertIn(f"{owner} retains responsibility", message)

    def test_invalid_receipts_fail_closed(self):
        for receipt in ({"status":"intent"}, None, {"status":"submitted",
                        "attempts":True,"next_attempt":0}, {"status":"submitted",
                        "attempts":1,"next_attempt":float("nan")}):
            with self.assertRaises(ValueError):
                validate_receipts({self.incident["id"]:receipt})


if __name__ == "__main__":
    unittest.main()
