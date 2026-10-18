"""Declarative hard and soft constraints.

Hard constraints are deal breakers: a violation is a veto, and the engine never relaxes
or downgrades one. Only the owner can change them via `set ...` + `confirm`.
Reasons carry a generic category and never the person's actual limits or diet.
"""
from dataclasses import dataclass
from typing import Callable, Optional

import ranking


@dataclass(frozen=True)
class Constraint:
    name: str
    kind: str        # 'hard' | 'soft'
    category: str    # generic, safe to share with the group
    check: Callable  # hard: (persona, c) -> bool satisfied; soft: (persona, c) -> utility or None
    reason: str = ''  # shared veto text (hard only)


# Non-negotiable checks first: only the first veto reason leaves the agent, and a
# negotiable reason must not hide one that no counteroffer could fix.
HARD = (
    Constraint('dietary', 'hard', 'dietary', lambda p, c: all(d in c.tags for d in p['hard']['dietary']), 'dietary requirement unavailable'),
    Constraint('free_slots', 'hard', 'availability', lambda p, c: c.slot in p['hard']['free_slots'], 'not free at that time'),
    Constraint('max_price', 'hard', 'budget', lambda p, c: c.price <= p['hard']['max_price'], 'over budget'),
    Constraint('max_distance_min', 'hard', 'distance', lambda p, c: c.distance_min <= p['hard']['max_distance_min'], 'too far'),
    Constraint('never_cuisines', 'hard', 'cuisine', lambda p, c: c.cuisine.lower() not in p['hard'].get('never_cuisines', []), 'cuisine is a deal breaker'),
)

SOFT = (
    Constraint('cuisine_pref', 'soft', 'cuisine', ranking.cuisine_utility),
    Constraint('rating', 'soft', 'rating', ranking.rating_utility),
    Constraint('comfort_distance_min', 'soft', 'distance', ranking.distance_utility),
    Constraint('comfort_price', 'soft', 'budget', ranking.price_utility),
    Constraint('preferred_slots', 'soft', 'availability', ranking.time_utility),
)

CATEGORY_BY_REASON = {k.reason: k.category for k in HARD}


def violations(persona, c):
    """All hard constraints this candidate breaks, in check order. Private to the agent."""
    return [k for k in HARD if not k.check(persona, c)]


def first_violation(persona, c) -> Optional[Constraint]:
    return next((k for k in HARD if not k.check(persona, c)), None)


def category_for_reason(reason):
    return CATEGORY_BY_REASON.get(reason, 'other')
