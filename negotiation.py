"""Counteroffers (nothing feasible) and compromise proposals (feasible but someone unhappy).

Pure functions. Organizer side sees only categories and scores; the size of any
relaxation is computed by minimal_relaxation() inside the owner's personal agent and is
applied only through the existing `set` + `confirm` flow. Wiring: docs/negotiation.md.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Tuple

import constraints
import fairness
from preferences import validate_update

# Diet is often religion or allergy: never ask anyone to bend it.
NON_NEGOTIABLE = {'dietary'}
UNHAPPY_MIN = 0.4     # fair pick leaves someone below this -> offer a compromise set
UNHAPPY_SPREAD = 0.4  # or the gap between happiest and least happy exceeds this
LOVE = 0.8

QUESTIONS = {
    'availability': 'Would you consider {name} on {when}?',
    'budget': 'Would you stretch your budget a little for {name} on {when}?',
    'distance': 'Would you travel a bit farther for {name} on {when}?',
    'cuisine': 'Would you make an exception on cuisine for {name} on {when}?',
}


@dataclass(frozen=True)
class NearMiss:
    candidate_id: str
    blockers: Dict[str, str]  # person -> category of the veto their agent reported
    others_min: float         # lowest score among people who already accept it


@dataclass(frozen=True)
class Counteroffer:
    person: str               # the only recipient
    candidate_id: str
    category: str
    question: str


@dataclass(frozen=True)
class Option:
    candidate_id: str
    line: str


@dataclass(frozen=True)
class Compromise:
    options: Tuple[Option, ...]
    rotation_credits: Dict[str, float]  # store these if the group takes the fair pick now


def when(slot):
    dt = datetime.fromisoformat(slot)
    minutes = f':{dt.minute:02d}' if dt.minute else ''
    return f"{dt:%b} {dt.day} at {dt.hour % 12 or 12}{minutes}{'am' if dt.hour < 12 else 'pm'}"


def near_misses(replies, limit=3) -> List[NearMiss]:
    """Vetoed candidates ranked by fewest blockers, then fewest categories, then how
    happy everyone else already is. Candidates with a non-negotiable veto are skipped."""
    blockers, scores = defaultdict(dict), defaultdict(dict)
    for person, reply in replies.items():
        for s in reply.scores:
            if s.veto:
                blockers[s.candidate_id][person] = constraints.category_for_reason(s.reason)
            else:
                scores[s.candidate_id][person] = s.score
    misses = [NearMiss(cid, b, min(scores[cid].values(), default=1.0))
              for cid, b in blockers.items() if not NON_NEGOTIABLE & set(b.values())]
    misses.sort(key=lambda m: (len(m.blockers), len(set(m.blockers.values())), -m.others_min, m.candidate_id))
    return misses[:limit]


def counteroffers(replies, candidates, limit=3) -> List[Counteroffer]:
    """At most one question per blocking person (their best near miss), so each owner
    faces a single decision. Never mentions anyone else."""
    by_id = {c.id: c for c in candidates}
    out, asked = [], set()
    for m in near_misses(replies, limit):
        c = by_id[m.candidate_id]
        for person, category in sorted(m.blockers.items()):
            if person in asked:
                continue
            asked.add(person)
            q = QUESTIONS.get(category, QUESTIONS['availability']).format(name=c.name, when=when(c.slot))
            out.append(Counteroffer(person, c.id, category, q + ' It would unlock a plan for the whole group.'))
    return out


def unlocked(misses, answers) -> Optional[str]:
    """First near miss whose every blocker accepted the counteroffer for it.
    `answers` maps (person, candidate_id) -> accepted."""
    return next((m.candidate_id for m in misses if all(answers.get((p, m.candidate_id)) for p in m.blockers)), None)


def minimal_relaxation(persona, c) -> Optional[dict]:
    """Personal-agent side, private. The smallest `set` updates that make c feasible,
    {} if it already is, or None if a non-negotiable or unknown constraint blocks it.
    Returned changes are only proposals: the owner must confirm them."""
    changes = {}
    for k in constraints.violations(persona, c):
        if k.category in NON_NEGOTIABLE:
            return None
        if k.name == 'free_slots':
            changes['free_slots'] = persona['hard']['free_slots'] + [c.slot]
        elif k.name == 'max_price':
            changes['max_price'] = c.price
        elif k.name == 'max_distance_min':
            changes['max_distance_min'] = c.distance_min
        elif k.name == 'never_cuisines':
            changes['never_cuisines'] = [x for x in persona['hard']['never_cuisines'] if x != c.cuisine.lower()]
        else:
            return None
    if changes:
        try:
            validate_update(persona, changes)
        except ValueError:
            return None
    return changes


def describe_relaxation(persona, changes) -> str:
    """Owner-only wording of a relaxation, e.g. '+$4 budget (to $34)'. Never send to the group."""
    h, parts = persona['hard'], []
    if 'max_price' in changes:
        parts.append(f"+${changes['max_price'] - h['max_price']:g} budget (to ${changes['max_price']:g})")
    if 'max_distance_min' in changes:
        parts.append(f"+{changes['max_distance_min'] - h['max_distance_min']:g} min travel (to {changes['max_distance_min']:g} min)")
    for slot in changes.get('free_slots', [])[len(h['free_slots']):]:
        parts.append(f'add {when(slot)} to your free times')
    if 'never_cuisines' in changes:
        parts += [f'allow {x}' for x in h.get('never_cuisines', []) if x not in changes['never_cuisines']]
    return '; '.join(parts) or 'no change needed'


def pareto_frontier(table) -> List[str]:
    """Candidates no other candidate beats for someone without hurting anyone."""
    def dominates(a, b):
        return all(a[p] >= b[p] for p in b) and any(a[p] > b[p] for p in b)
    return sorted(c for c in table if not any(dominates(table[o], table[c]) for o in table if o != c))


def tradeoff(scores) -> str:
    lovers, lowest = sum(s >= LOVE for s in scores.values()), min(scores.values())
    if lovers == len(scores):
        return 'everyone loves it'
    if not lovers:
        return f'everyone ≥ {lowest:.2f}'
    return f"{lovers} {'person loves' if lovers == 1 else 'people love'} it, lowest is {lowest:.2f}"


def compromise_set(table, decision=None, names=None, k=3) -> List[Option]:
    """Top k Pareto-optimal options in fairness order, each with a one-line tradeoff."""
    decision = decision or fairness.decide(table)
    front = set(pareto_frontier(table))
    label = lambda cid: (names or {}).get(cid, cid)
    picks = [c for c in decision.ranking if c in front][:k]
    return [Option(c, f"{chr(65 + i)}: {label(c)}: {tradeoff(table[c])}{' (fair pick)' if c == decision.chosen else ''}")
            for i, c in enumerate(picks)]


def needs_compromise(decision, min_floor=UNHAPPY_MIN, max_spread=UNHAPPY_SPREAD) -> bool:
    return decision.metrics['min'] < min_floor or decision.metrics['spread'] > max_spread


def propose_compromise(table, history=None, names=None, k=3) -> Optional[Compromise]:
    """None when the fair pick already works for everyone; otherwise options to vote on,
    plus the rotation credits to bank if the group simply takes the fair pick."""
    d = fairness.decide(table, history=history, names=names)
    if not needs_compromise(d):
        return None
    return Compromise(tuple(compromise_set(table, d, names, k)), fairness.update_credits(history, d))
