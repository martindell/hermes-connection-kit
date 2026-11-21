import copy
import unittest

import baton as b


class BatonTests(unittest.TestCase):
    def setUp(self):
        self.state = b.empty("example")

    def record(self, action, actor="work", **extra):
        seq = self.state["tasks"].get("task-1", {}).get("seq", 0) + 1
        return dict(id=f"event-{seq}", project="example", task="task-1",
                    seq=seq, actor=actor, action=action, at=seq * 10, **extra)

    def send(self, action, actor="work", **extra):
        record = self.record(action, actor, **extra)
        self.state = b.apply(self.state, record)
        return record

    def opened(self):
        return self.send("open", deadline=100, source="issue:1:comment:2")

    def offered(self):
        self.opened()
        return self.send("offer", to="hermes", accept_by=50,
                         next_deadline=200, evidence="accepted-run-receipt:3")

    def accepted(self):
        offer = self.offered()
        return self.send("accept", "hermes", offer_id=offer["id"], evidence="run:receipt")

    def test_pending_handover_retains_sender(self):
        self.offered()
        self.assertEqual(b.due(self.state, 50)[0]["owner"], "work")
        self.assertEqual(b.due(self.state, 50)[0]["kind"], "handover-unaccepted")

    def test_received_handover_does_not_finish_work(self):
        self.accepted()
        self.assertEqual(b.due(self.state, 200)[0]["owner"], "hermes")
        offer = self.send("offer", "hermes", to="work", accept_by=80,
                          next_deadline=150, evidence="result:4")
        self.send("accept", offer_id=offer["id"], evidence="review-started:5")
        self.assertEqual(b.due(self.state, 150)[0]["owner"], "work")
        self.send("finish", result="review:6", user_update="conversation:reply:7")
        self.assertEqual(b.due(self.state, 1000), [])

    def test_identical_replay_is_noop(self):
        record = self.opened()
        self.assertEqual(b.apply(self.state, record), self.state)

    def test_mutated_replay_conflicts(self):
        record = self.opened()
        record["deadline"] = 1000
        with self.assertRaises(b.Conflict):
            b.apply(self.state, record)

    def test_concurrent_writer_cannot_overwrite_owner(self):
        self.opened()
        stale = self.record("wait_user", evidence="question:1")
        self.send("update", deadline=200, evidence="progress:1")
        with self.assertRaises(b.Conflict):
            b.apply(self.state, stale)

    def test_user_wait_does_not_alarm(self):
        self.opened()
        self.send("wait_user", evidence="conversation:question:1")
        self.assertEqual(b.due(self.state, 1000000), [])
        self.send("resume", deadline=100, evidence="conversation:answer:2")
        self.assertEqual(b.due(self.state, 100)[0]["owner"], "work")

    def test_wrong_receiver_or_offer_rejected(self):
        offer = self.offered()
        for actor, offer_id in [("work", offer["id"]), ("hermes", "wrong")]:
            with self.assertRaises(b.Conflict):
                b.apply(self.state, self.record("accept", actor, offer_id=offer_id,
                                                evidence="receipt:1"))

    def test_sender_cannot_extend_pending_offer(self):
        self.offered()
        with self.assertRaises(b.Conflict):
            self.send("update", deadline=500, evidence="progress:1")

    def test_invalid_event_leaves_state_unchanged(self):
        self.opened()
        before = copy.deepcopy(self.state)
        with self.assertRaises(b.Conflict):
            self.send("update", deadline=200, evidence="")
        self.assertEqual(self.state, before)

    def test_deadlines_reject_nan_infinity_boolean_and_past(self):
        for value in [float("nan"), float("inf"), True, -1, 10]:
            with self.subTest(value=value), self.assertRaises(b.Conflict):
                self.send("open", deadline=value, source="source:1")

    def test_hermes_cannot_finish_or_cancel(self):
        self.accepted()
        for action, fields in [("finish", dict(result="r", user_update="u")),
                               ("cancel", dict(evidence="e"))]:
            with self.assertRaises(b.Conflict):
                self.send(action, "hermes", **fields)

    def test_wrong_project_unknown_fields_and_action_rejected(self):
        self.opened()
        base = self.record("update", deadline=200, evidence="e")
        for changes in [dict(project="other"), dict(command="reboot"),
                        dict(action="restart"), dict(at=0)]:
            with self.assertRaises(b.Conflict):
                b.apply(self.state, dict(base, **changes))

    def test_serialized_restart_keeps_obligation(self):
        import json
        self.offered()
        restored = json.loads(b.canonical(self.state))
        self.assertEqual(b.due(restored, 100), b.due(self.state, 100))

    def test_late_acceptance_does_not_manufacture_new_deadline(self):
        offer = self.offered()
        record = self.record("accept", "hermes", offer_id=offer["id"], evidence="r")
        record["at"] = 300
        self.state = b.apply(self.state, record)
        self.assertEqual(b.due(self.state, 300)[0]["owner"], "hermes")

    def test_closed_task_cannot_reopen(self):
        self.opened()
        self.send("cancel", evidence="user:cancelled")
        self.assertEqual(b.due(self.state, 500), [])
        with self.assertRaises(b.Conflict):
            self.send("open", deadline=600, source="new")

    def test_work_can_record_authorised_cancellation_after_hermes_failure(self):
        self.accepted()
        self.send("cancel", evidence="source:explicit-cancellation")
        task = self.state["tasks"]["task-1"]
        self.assertEqual(task["phase"], "cancelled")
        self.assertIsNone(task["offer"])
        self.assertEqual(b.due(self.state, 1000), [])

    def test_cancellation_clears_pending_handover(self):
        self.offered()
        self.send("cancel", evidence="source:explicit-cancellation")
        self.assertIsNone(self.state["tasks"]["task-1"]["offer"])
        self.assertEqual(b.due(self.state, 1000), [])


if __name__ == "__main__":
    unittest.main()
