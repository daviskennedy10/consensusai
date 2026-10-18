import time
import unittest
from unittest.mock import patch

from coordination import Plan
from models import Candidate, CandidateScore, ScoreReply
from scoring import build_complete_table


class AgentWorkflowTests(unittest.TestCase):

    def setUp(self):
        self.participants = {
            "alex": "address-alex",
            "maya": "address-maya",
            "jordan": "address-jordan",
            "sam": "address-sam",
        }

        self.candidate = Candidate(
            id="med-grill@2026-10-25T18:00",
            name="Med Grill",
            cuisine="mediterranean",
            price=22,
            distance_min=15,
            rating=4.5,
            tags=["halal", "vegetarian"],
            slot="2026-10-25T18:00",
        )

        self.plan = Plan(
            user="test-user",
            participants=self.participants,
            coordinator="address-alex",
        )

        self.plan.begin_round([self.candidate])

    def make_reply(self, person, veto=False, score=0.7):
        return ScoreReply(
            plan_id=self.plan.id,
            round=self.plan.round,
            persona=person,
            scores=[
                CandidateScore(
                    candidate_id=self.candidate.id,
                    veto=veto,
                    score=score,
                    reason="test evaluation",
                )
            ],
        )

    def test_missing_agent_prevents_decision(self):
        """Three out of four replies must never produce a decision."""

        for person in ("alex", "maya", "jordan"):
            self.plan.replies[person] = self.make_reply(person)

        self.assertFalse(self.plan.is_complete())
        self.assertEqual(self.plan.missing_agents(), ["sam"])

        table = build_complete_table(
            self.plan.replies,
            self.plan.candidates,
            self.plan.participants,
        )

        self.assertIsNone(table)

    def test_fresh_round_replaces_old_vetoes(self):
        """After an approval, a fresh round must collect new scores."""

        # Jordan initially vetoes the candidate.
        for person in self.participants:
            self.plan.replies[person] = self.make_reply(
                person,
                veto=(person == "jordan"),
            )

        old_table = build_complete_table(
            self.plan.replies,
            self.plan.candidates,
            self.plan.participants,
        )

        self.assertEqual(old_table, {})

        # Simulate the coordinator restarting scoring after approval.
        self.plan.negotiation_attempted = True
        self.plan.begin_round([self.candidate])

        # Old replies must not survive the restart.
        self.assertEqual(self.plan.replies, {})
        self.assertFalse(self.plan.is_complete())
        self.assertFalse(self.plan.finalizing)

        # All four agents rescore with updated preferences.
        for person in self.participants:
            self.plan.replies[person] = self.make_reply(
                person,
                veto=False,
            )

        new_table = build_complete_table(
            self.plan.replies,
            self.plan.candidates,
            self.plan.participants,
        )

        self.assertIsNotNone(new_table)
        self.assertIn(self.candidate.id, new_table)
        self.assertEqual(len(new_table[self.candidate.id]), 4)

    def test_timeout_requires_expired_scoring_window(self):
        """A fresh round gets a fresh timeout window."""

        self.assertFalse(self.plan.expired(12))

        self.plan.t0 = time.monotonic() - 20
        self.assertTrue(self.plan.expired(12))

        self.plan.begin_round([self.candidate])
        self.assertFalse(self.plan.expired(12))


if __name__ == "__main__":
    unittest.main()