import unittest

from activity_trace import ActivityTrace, EventType


class ActivityTraceTests(unittest.TestCase):

    def setUp(self):
        self.trace = ActivityTrace(plan_id="abc123")

    def test_events_have_sequential_numbers(self):
        first = self.trace.record(
            EventType.PLAN_STARTED, "alex"
        )
        second = self.trace.record(
            EventType.SCORE_REQUESTED, "alex", target="maya"
        )

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(len(self.trace.events), 2)

    def test_renders_agent_communication(self):
        self.trace.record(EventType.PLAN_STARTED, "alex")
        self.trace.record(
            EventType.SCORE_REQUESTED, "alex", target="maya"
        )
        self.trace.record(EventType.SCORE_RECEIVED, "maya")

        output = self.trace.render()

        self.assertIn("PLAN abc123", output)
        self.assertIn(
            "Alex requested an evaluation from Maya",
            output,
        )
        self.assertIn(
            "Maya submitted an evaluation",
            output,
        )

    def test_private_information_cannot_be_added_as_actor(self):
        with self.assertRaises(ValueError):
            self.trace.record(
                EventType.SCORE_RECEIVED,
                "Maya's private budget is $20",
            )

    def test_rejects_unknown_events(self):
        with self.assertRaises(ValueError):
            self.trace.record("private_preference_dump", "alex")

    def test_rejects_unknown_targets(self):
        with self.assertRaises(ValueError):
            self.trace.record(
                EventType.COUNTEROFFER_SENT,
                "alex",
                target="private@example.com",
            )

    def test_approval_and_rescoring(self):
        self.trace.record(
            EventType.COUNTEROFFER_SENT,
            "alex",
            target="jordan",
        )
        self.trace.record(
            EventType.APPROVAL_REQUIRED,
            "jordan",
        )
        self.trace.record(EventType.APPROVED, "jordan")
        self.trace.record(
            EventType.RESCORING_STARTED,
            "system",
        )
        self.trace.record(
            EventType.PLAN_PROPOSED,
            "alex",
        )

        output = self.trace.render()

        self.assertIn("owner approved", output)
        self.assertIn("evaluate the updated plan", output)
        self.assertIn("feasible group proposal", output)

    def test_empty_trace(self):
        self.assertIn(
            "No activity recorded.",
            self.trace.render(),
        )

    def test_invalid_round_rejected(self):
        with self.assertRaises(ValueError):
            self.trace.record(
                EventType.SCORE_REQUESTED,
                "alex",
                target="maya",
                round=0,
            )


if __name__ == "__main__":
    unittest.main()