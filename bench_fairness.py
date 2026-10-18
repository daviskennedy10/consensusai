"""Fairness vs averaging on random groups. Run: python bench_fairness.py [n] [seed]

Each scenario: 2-10 people with random deal breakers, 10-25 venues over 3 slots (about
one page of a Places/Yelp search), scored by the real scoring.evaluate and gated by
build_complete_table, exactly as the organizer does. Infeasible scenarios are skipped.
Two taste models: 'random' (independent tastes) and 'majority' (everyone shares a taste
profile except one person whose tastes are the opposite).
"""
import random
import sys
from statistics import mean
from types import SimpleNamespace

import fairness
from scoring import build_complete_table, evaluate

SLOTS = ['2026-10-23T19:00', '2026-10-24T19:00', '2026-10-25T18:00']
CUISINES = ['thai', 'italian', 'burger', 'fusion', 'mediterranean', 'mexican', 'sushi', 'steak']
DIETS = ['vegetarian', 'halal']


def random_persona(rng, taste=None):
    return {'hard': {'max_price': rng.randint(20, 60), 'max_distance_min': rng.randint(10, 40),
                     'dietary': [d for d in DIETS if rng.random() < 0.15],
                     'free_slots': rng.sample(SLOTS, rng.randint(2, 3))},
            'cuisine_pref': taste or {c: round(rng.random(), 2) for c in rng.sample(CUISINES, rng.randint(3, 6))},
            'weights': {k: rng.randint(1, 5) for k in ('cuisine', 'rating', 'distance', 'price')}}


def majority_tastes(rng, size):
    shared = {c: rng.random() for c in CUISINES}
    noisy = lambda: {c: round(min(1, max(0, v + rng.uniform(-0.1, 0.1))), 2) for c, v in shared.items()}
    return [noisy() for _ in range(size - 1)] + [{c: round(1 - v, 2) for c, v in shared.items()}]


def random_scenario(rng, tastes='random'):
    size = rng.randint(2, 10)
    taste = majority_tastes(rng, size) if tastes == 'majority' else [None] * size
    people = {f'p{i}': random_persona(rng, taste[i]) for i in range(size)}
    venues = [dict(id=f'v{j}', name=f'Venue {j}', cuisine=rng.choice(CUISINES), price=rng.randint(10, 50),
                   distance_min=rng.randint(3, 35), rating=round(rng.uniform(3.0, 5.0), 1),
                   tags=[d for d in DIETS if rng.random() < 0.6]) for j in range(rng.randint(10, 25))]
    cands = [SimpleNamespace(**{**v, 'id': f"{v['id']}@{s}"}, slot=s) for v in venues for s in SLOTS]
    return people, cands


def score_table(people, cands):
    replies = {}
    for name, p in people.items():
        scores = []
        for c in cands:
            veto, score, _ = evaluate(p, c)
            scores.append(SimpleNamespace(candidate_id=c.id, veto=veto, score=score))
        replies[name] = SimpleNamespace(scores=scores)
    return build_complete_table(replies, cands, people)


def simulate(n=1000, seed=0, objectives=('leximin',), tastes='random'):
    """Per feasible scenario: {objective: (min, mean, gini)} plus 'utilitarian' and the raw inputs."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        people, cands = random_scenario(rng, tastes)
        table = score_table(people, cands)
        if not table:
            continue
        row = {'people': people, 'cands': {c.id: c for c in cands}, 'chosen': {}, 'options': len(table)}
        for obj in set(objectives) | {'utilitarian'}:
            d = fairness.decide(table, obj)
            row['chosen'][obj] = d.chosen
            row[obj] = (d.metrics['min'], d.metrics['mean'], d.metrics['gini'])
        rows.append(row)
    return rows


def summary(rows, objective):
    base = [r['utilitarian'] for r in rows]
    fair = [r[objective] for r in rows]
    improved = [f[0] - b[0] for f, b in zip(fair, base) if f[0] > b[0] + 1e-9]
    return {'improved_pct': 100 * len(improved) / len(rows),
            'worst_gain_when_improved': mean(improved) if improved else 0.0,
            'worst_gain_all': mean(f[0] - b[0] for f, b in zip(fair, base)),
            'mean_cost': mean(b[1] - f[1] for f, b in zip(fair, base)),
            'gini': mean(f[2] for f in fair), 'gini_avg': mean(b[2] for b in base)}


def main(n=500, seed=0):
    objectives = ('leximin', 'maximin', 'nash')
    print(f'{n} random groups of 2-10 people per taste model, seed {seed}. Only groups with 2+ feasible '
          'options count (otherwise there is nothing to choose). Baseline = highest-average pick.\n')
    print('| Tastes | Objective | Groups | Worst-off better off | Worst-off gain (when better) | Worst-off gain (all) | Cost to mean | Gini (fair vs avg) |')
    print('|---|---|---|---|---|---|---|---|')
    for tastes in ('random', 'majority'):
        choice = [r for r in simulate(n, seed, objectives, tastes) if r['options'] > 1]
        for obj in objectives:
            s = summary(choice, obj)
            print(f"| {tastes} | {obj} | {len(choice)} | {s['improved_pct']:.1f}% | +{s['worst_gain_when_improved']:.3f} "
                  f"| +{s['worst_gain_all']:.3f} | -{s['mean_cost']:.3f} | {s['gini']:.3f} vs {s['gini_avg']:.3f} |")


if __name__ == '__main__':
    main(*(int(a) for a in sys.argv[1:3]))
