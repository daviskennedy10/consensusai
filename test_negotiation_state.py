import unittest

from negotiation_state import (
    NegotiationSession,
    NegotiationStatus,
)


class NegotiationStateTests(unittest.TestCase):

    def make_session(self, people=("maya", "jordan")):
        return NegotiationSession(
            plan_id="plan123",
            candidate_id="med-grill@2026-10-25T18:00",
            required_people=set(people),
        )

    def test_all_approvals_required(self):
        session = self.make_session()

        self.assertEqual(
            session.respond("maya", True),
            "waiting",
        )

        self.assertEqual(
            session.respond("jordan", True),
            "rescore",
        )

        self.assertEqual(
            session.status,
            NegotiationStatus.RESCORING,
        )

    def test_rejection_stops_negotiation(self):
        session = self.make_session()

        self.assertEqual(
            session.respond("maya", False),
            "rejected",
        )

        self.assertEqual(
            session.respond("jordan", True),
            "ignored",
        )

    def test_duplicate_approval_does_not_rescore(self):
        session = self.make_session(("maya",))

        self.assertEqual(
            session.respond("maya", True),
            "rescore",
        )

        self.assertEqual(
            session.respond("maya", True),
            "ignored",
        )

    def test_unknown_participant_cannot_approve(self):
        session = self.make_session()

        self.assertEqual(
            session.respond("sam", True),
            "unauthorized",
        )

        self.assertEqual(session.approved, set())

    def test_expired_approval_is_rejected(self):
        session = self.make_session(("maya",))

        result = session.respond(
            "maya",
            True,
            now=session.created_at + 181,
        )

        self.assertEqual(result, "expired")
        self.assertEqual(
            session.status,
            NegotiationStatus.EXPIRED,
        )

    def test_cannot_complete_before_rescoring(self):
        session = self.make_session()

        with self.assertRaises(ValueError):
            session.mark_complete()

    def test_complete_after_rescoring(self):
        session = self.make_session(("maya",))

        session.respond("maya", True)
        session.mark_complete()

        self.assertEqual(
            session.status,
            NegotiationStatus.COMPLETE,
        )


if __name__ == "__main__":
    unittest.main()