"""Safe, testable negotiation session lifecycle.

No network calls, database operations, or preference changes happen here.
The personal agents remain responsible for authorization and persistence.
"""

import time
from dataclasses import dataclass, field
from enum import Enum


class NegotiationStatus(str, Enum):
    WAITING = "waiting"
    RESCORING = "rescoring"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMPLETE = "complete"


@dataclass
class NegotiationSession:
    plan_id: str
    candidate_id: str
    required_people: set[str]
    timeout: float = 180.0

    created_at: float = field(default_factory=time.monotonic)
    status: NegotiationStatus = NegotiationStatus.WAITING
    approved: set[str] = field(default_factory=set)
    rejected: set[str] = field(default_factory=set)

    def expired(self, now=None) -> bool:
        now = time.monotonic() if now is None else now

        return now - self.created_at >= self.timeout

    def respond(self, person: str, approved: bool, now=None) -> str:
        """Record an authenticated participant's decision.

        Caller must verify the agent sender and the plan/candidate IDs.
        """

        if self.status != NegotiationStatus.WAITING:
            return "ignored"

        if self.expired(now):
            self.status = NegotiationStatus.EXPIRED
            return "expired"

        if person not in self.required_people:
            return "unauthorized"

        if person in self.approved or person in self.rejected:
            return "duplicate"

        if not approved:
            self.rejected.add(person)
            self.status = NegotiationStatus.REJECTED
            return "rejected"

        self.approved.add(person)

        if self.approved == self.required_people:
            self.status = NegotiationStatus.RESCORING
            return "rescore"

        return "waiting"

    def mark_complete(self):
        if self.status != NegotiationStatus.RESCORING:
            raise ValueError(
                "Cannot complete negotiation before rescoring"
            )

        self.status = NegotiationStatus.COMPLETE