"""Privacy-safe event tracing for ConsensusAI agent coordination.

Records sanitized, structured events rather than arbitrary messages.
No private preferences, raw agent payloads, or user messages are stored.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EventType(str, Enum):
    PLAN_STARTED = "plan_started"
    SCORE_REQUESTED = "score_requested"
    SCORE_RECEIVED = "score_received"
    ROUND_RETRY = "round_retry"
    NO_FEASIBLE_PLAN = "no_feasible_plan"
    COUNTEROFFER_SENT = "counteroffer_sent"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESCORING_STARTED = "rescoring_started"
    PLAN_PROPOSED = "plan_proposed"
    AGENT_TIMEOUT = "agent_timeout"
    NEGOTIATION_TIMEOUT = "negotiation_timeout"


@dataclass(frozen=True)
class ActivityEvent:
    sequence: int
    kind: EventType
    actor: str
    target: str | None
    round: int | None
    timestamp: datetime


@dataclass
class ActivityTrace:
    plan_id: str
    events: list[ActivityEvent] = field(default_factory=list)

    def record(
        self,
        kind: EventType,
        actor: str,
        target: str | None = None,
        round: int | None = None,
    ) -> ActivityEvent:
        """Accept only known event types and public agent names."""
        if not isinstance(kind, EventType):
            raise ValueError("Unknown event type")

        allowed = {"alex", "maya", "jordan", "sam", "system"}

        if actor not in allowed:
            raise ValueError("Unknown actor")

        if target is not None and target not in allowed:
            raise ValueError("Unknown target")

        if round is not None and (type(round) is not int or round < 1):
            raise ValueError("Invalid round")

        event = ActivityEvent(
            sequence=len(self.events) + 1,
            kind=kind,
            actor=actor,
            target=target,
            round=round,
            timestamp=datetime.now(timezone.utc),
        )

        self.events.append(event)
        return event

    def render(self) -> str:
        """Show a readable, chronological activity transcript."""
        lines = [
            f"CONSENSUSAI — PLAN {self.plan_id}",
            "",
        ]

        for event in self.events:
            description = describe(event)

            lines.append(
                f"{event.sequence:02d}  {description}"
            )

        if not self.events:
            lines.append("No activity recorded.")

        return "\n".join(lines)


def describe(event: ActivityEvent) -> str:
    actor = event.actor.title()
    target = event.target.title() if event.target else None

    descriptions = {
        EventType.PLAN_STARTED:
            f"{actor} started coordinating the group plan.",

        EventType.SCORE_REQUESTED:
            f"{actor} requested an evaluation from {target}.",

        EventType.SCORE_RECEIVED:
            f"{actor} submitted an evaluation.",

        EventType.ROUND_RETRY:
            "The group started another search round.",

        EventType.NO_FEASIBLE_PLAN:
            "No unanimously feasible restaurant was found.",

        EventType.COUNTEROFFER_SENT:
            f"{actor} requested a private counteroffer from {target}.",

        EventType.APPROVAL_REQUIRED:
            f"{actor}'s agent requested human approval privately.",

        EventType.APPROVED:
            f"{actor}'s owner approved a proposed preference change.",

        EventType.REJECTED:
            f"{actor}'s owner rejected a proposed preference change.",

        EventType.RESCORING_STARTED:
            "All agents were asked to evaluate the updated plan.",

        EventType.PLAN_PROPOSED:
            "The coordinator produced a feasible group proposal.",

        EventType.AGENT_TIMEOUT:
            "An agent failed to respond before the scoring deadline.",

        EventType.NEGOTIATION_TIMEOUT:
            "A negotiation expired while awaiting human approval.",
    }

    return descriptions[event.kind]