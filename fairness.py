"""Group decision objectives over a feasible score table {candidate_id: {person: score}}.

Every row must already be feasible for everyone (see scoring.build_complete_table);
this module only ranks. Ties break deterministically on candidate id (largest wins,
matching the legacy choose()).

Fairness over time: `history` maps person -> fairness credit. During selection a
person counts as min(credit, CREDIT_CAP) worse off than they are, so leximin/maximin/nash
prioritize them (utilitarian is unaffected). Reported metrics always use raw scores.
update_credits() grows credit with regret and decays it, so it stays in [0, CREDIT_CAP].
That shape maps 1:1 onto a fairness_credits(person, credit) table.
"""
from dataclasses import dataclass, field
from math import log
from statistics import mean
from typing import Dict, Mapping, Optional, Tuple

OBJECTIVES = ('leximin', 'maximin', 'nash', 'utilitarian')
NASH_EPS = 1e-3
CREDIT_CAP = 0.1   # max priority a past loser gets; small enough to only break near-ties
CREDIT_DECAY = 0.5
CREDIT_RATE = 0.5  # share of today's regret banked as credit


@dataclass(frozen=True)
class Decision:
    chosen: str
    ranking: Tuple[str, ...]  # all candidates, best first under the objective
    average_id: str           # utilitarian pick on raw scores, for comparison
    objective: str
    metrics: Dict = field(default_factory=dict)
    explanation: str = ''


def _key(objective, scores, cid):
    vals = [round(v, 9) for v in scores.values()]
    avg = round(mean(vals), 9)
    if objective == 'leximin':
        return (tuple(sorted(vals)), avg, cid)
    if objective == 'maximin':
        return (min(vals), avg, cid)
    if objective == 'nash':
        return (round(sum(log(max(v, 0.0) + NASH_EPS) for v in vals), 9), min(vals), cid)
    if objective == 'utilitarian':
        return (avg, cid)
    raise ValueError(f'Unknown objective {objective!r}; use one of {OBJECTIVES}')


def _prioritize(scores, history):
    if not history:
        return scores
    return {p: s - min(max(history.get(p, 0.0), 0.0), CREDIT_CAP) for p, s in scores.items()}


def rank(table, objective='leximin', history=None):
    if not table:
        raise ValueError('No feasible candidates')
    return tuple(sorted(table, key=lambda c: _key(objective, _prioritize(table[c], history), c), reverse=True))


def gini(values):
    vals = list(values)
    m = mean(vals)
    if m <= 0:
        return 0.0
    return sum(abs(a - b) for a in vals for b in vals) / (2 * len(vals) ** 2 * m)


def metrics(table, cid):
    scores = table[cid]
    best = {p: max(row[p] for row in table.values()) for p in scores}
    return {'min': round(min(scores.values()), 3), 'mean': round(mean(scores.values()), 3),
            'spread': round(max(scores.values()) - min(scores.values()), 3),
            'gini': round(gini(scores.values()), 3),
            'regret': {p: round(best[p] - s, 3) for p, s in scores.items()}}


def decide(table: Mapping[str, Mapping[str, float]], objective: str = 'leximin',
           history: Optional[Mapping[str, float]] = None, names: Optional[Mapping[str, str]] = None) -> Decision:
    order = rank(table, objective, history)
    chosen, avg = order[0], rank(table, 'utilitarian')[0]
    m = metrics(table, chosen)
    label = lambda cid: (names or {}).get(cid, cid)
    avg_min = min(table[avg].values())
    if chosen == avg:
        text = f"{label(chosen)} keeps everyone at {m['min']:.2f} or higher and also has the highest average ({m['mean']:.2f})."
    elif avg_min < m['min']:
        text = (f"{label(chosen)} keeps everyone at {m['min']:.2f} or higher; the highest-average option, "
                f"{label(avg)}, would leave someone at {avg_min:.2f}.")
    else:
        text = f"{label(chosen)} gives the best {objective} balance: lowest {m['min']:.2f}, average {m['mean']:.2f}."
    if history and chosen != rank(table, objective)[0]:
        text += ' Fairness credit from past outings tipped the balance.'
    return Decision(chosen, order, avg, objective, m, text)


def update_credits(history: Optional[Mapping[str, float]], decision: Decision) -> Dict[str, float]:
    """Credits after taking `decision`: whoever compromised (regret > 0) banks some credit."""
    history = history or {}
    people = set(history) | set(decision.metrics['regret'])
    return {p: round(min(CREDIT_CAP, CREDIT_DECAY * history.get(p, 0.0) + CREDIT_RATE * decision.metrics['regret'].get(p, 0.0)), 4)
            for p in sorted(people)}
