from typing import List

from uagents import Model


class Candidate(Model):
    id: str            # "<venue_id>@<slot>", unique per venue and time
    name: str
    cuisine: str
    price: int         # USD per person
    distance_min: int  # travel minutes
    rating: float      # 0..5
    tags: List[str]    # e.g. ["vegetarian", "halal"]
    slot: str          # ISO local start time, e.g. "2026-10-24T19:00"


class ScoreRequest(Model):
    plan_id: str
    round: int
    candidates: List[Candidate]


class CandidateScore(Model):
    candidate_id: str
    veto: bool         # True = a hard constraint (deal breaker) is violated
    score: float       # 0..1 from soft preferences, only meaningful if not vetoed
    reason: str


class ScoreReply(Model):
    plan_id: str
    round: int
    persona: str
    scores: List[CandidateScore]


class CounterofferRequest(Model):
    plan_id: str
    candidate: Candidate
    category: str


class CounterofferReply(Model):
    plan_id: str
    persona: str
    candidate_id: str
    status: str
    # "approval_required", "not_possible", "approved", "rejected"