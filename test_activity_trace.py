import unittest
from dataclasses import fields
from datetime import timezone

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
        self.assertIn("fresh evaluation process started", output)
        self.assertIn("feasible group proposal", output)

    def test_empty_trace(self):
        self.assertIn(
            "No activity recorded.",
            self.trace.render(),
        )

    def test_invalid_round_rejected(self):
        for value in (0, -1, True, 1.5, "private text"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.trace.record(EventType.SCORE_REQUESTED, "alex", target="maya", round=value)

    def test_event_fields_and_utc_timestamp(self):
        event = self.trace.record(EventType.PLAN_STARTED, "alex")
        self.assertEqual({f.name for f in fields(event)},
                         {"sequence", "kind", "actor", "target", "round", "timestamp"})
        self.assertEqual(event.timestamp.tzinfo, timezone.utc)
        with self.assertRaises(TypeError):
            self.trace.record(EventType.PLAN_STARTED, "alex", message="private text")

    def test_render_preserves_observation_order(self):
        self.trace.record(EventType.PLAN_STARTED, "alex")
        self.trace.record(EventType.SCORE_RECEIVED, "maya")
        self.trace.record(EventType.PLAN_PROPOSED, "alex")
        lines = self.trace.render().splitlines()
        self.assertTrue(lines[2].startswith("01  Alex started"))
        self.assertTrue(lines[3].startswith("02  Maya submitted"))
        self.assertTrue(lines[4].startswith("03  The coordinator produced"))

    def test_descriptions_do_not_invent_human_or_delivery_actions(self):
        for kind in (EventType.REJECTED, EventType.APPROVAL_REQUIRED,
                     EventType.RESCORING_STARTED, EventType.NEGOTIATION_TIMEOUT):
            self.trace.record(kind, "maya")
        rendered = self.trace.render()
        self.assertIn("declined or could not approve", rendered)
        self.assertIn("reports that human approval is required", rendered)
        self.assertIn("review deadline expired", rendered)
        self.assertNotIn("owner rejected", rendered)
        self.assertNotIn("All agents were asked", rendered)
        self.assertNotIn("received", rendered)


if __name__ == "__main__":
    unittest.main()
