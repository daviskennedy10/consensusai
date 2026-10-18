"""Reusable planning-session state for ConsensusAI."""

import time
from dataclasses import dataclass, field
from uuid import uuid4

from models import Candidate, ScoreRequest


@dataclass
class Plan:
    user: str
    participants: dict[str, str]
    coordinator: str
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    round: int = 1
    replies: dict = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    t0: float = field(default_factory=time.monotonic)
    finalizing: bool = False

    def missing_agents(self) -> list[str]:
        return [
            name
            for name in self.participants
            if name not in self.replies
        ]

    def is_complete(self) -> bool:
        return len(self.missing_agents()) == 0

    def make_request(self) -> ScoreRequest:
        return ScoreRequest(
            plan_id=self.id,
            round=self.round,
            candidates=self.candidates,
        )

    def begin_round(self, candidates: list[Candidate]):
        self.candidates = candidates
        self.replies = {}
        self.t0 = time.monotonic()
        self.finalizing = False

    def expired(self, timeout: float) -> bool:
        return time.monotonic() - self.t0 >= timeout