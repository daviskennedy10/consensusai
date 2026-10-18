import copy
import json
import random
import unittest
from pathlib import Path
from types import SimpleNamespace

import negotiation
from negotiation import (compromise_set, counteroffers, describe_relaxation, minimal_relaxation,
                         near_misses, pareto_frontier, propose_compromise)
from preferences import validate_update
from scoring import evaluate

BASE = Path(__file__).resolve().parent
PERSONAS = json.loads((BASE/'personas.json').read_text())
VENUES = json.loads((BASE/'venues.json').read_text())


def candidates(slots):
    return [SimpleNamespace(**{**v, 'id': f"{v['id']}@{s}"}, slot=s) for v in VENUES for s in slots]


def replies_for(personas, cands):
    out = {}
    for name, p in personas.items():
        scores = []
        for c in cands:
            veto, score, reason = evaluate(p, c)
            scores.append(SimpleNamespace(candidate_id=c.id, veto=veto, score=score, reason=reason))
        out[name] = SimpleNamespace(scores=scores)
    return out


class CounterofferTests(unittest.TestCase):
    def setUp(self):
        self.cands = candidates(['2026-10-23T19:00'])  # Sam is not free that night
        self.replies = replies_for(PERSONAS, self.cands)

    def test_scenario_has_no_feasible_plan(self):
        self.assertTrue(all(any(s.veto for r in self.replies.values() for s in r.scores if s.candidate_id == c.id)
                            for c in self.cands))

    def test_near_miss_is_blocked_by_fewest_people(self):
        best = near_misses(self.replies)[0]
        self.assertEqual(best.candidate_id, 'garden-fusion@2026-10-23T19:00')  # others like it most
        self.assertEqual(best.blockers, {'sam': 'availability'})
        counts = [len(m.blockers) for m in near_misses(self.replies, limit=10)]
        self.assertEqual(counts, sorted(counts))

    def test_counteroffer_goes_only_to_blocker_and_leaks_nothing(self):
        offers = counteroffers(self.replies, self.cands)
        first = offers[0]
        self.assertEqual(first.person, 'sam')
        self.assertEqual(first.question, 'Would you consider Garden Fusion on Oct 23 at 7pm? It would unlock a plan for the whole group.')
        for o in offers:
            miss = next(m for m in near_misses(self.replies) if m.candidate_id == o.candidate_id)
            self.assertIn(o.person, miss.blockers)
            self.assertNotIn('$', o.question)
            self.assertFalse(any(name in o.question.lower() for name in PERSONAS))

    def test_each_person_gets_at_most_one_question(self):
        people = [o.person for o in counteroffers(self.replies, self.cands, limit=10)]
        self.assertEqual(len(people), len(set(people)))

    def test_unlocked_needs_every_blocker(self):
        misses = [negotiation.NearMiss('x', {'a': 'budget', 'b': 'distance'}, .5),
                  negotiation.NearMiss('y', {'a': 'availability'}, .4)]
        self.assertIsNone(negotiation.unlocked(misses, {('a', 'x'): True}))
        self.assertEqual(negotiation.unlocked(misses, {('a', 'x'): True, ('b', 'x'): True}), 'x')
        self.assertIsNone(negotiation.unlocked(misses, {('a', 'y'): False}))
        self.assertEqual(negotiation.unlocked(misses, {('a', 'y'): True}), 'y')

    def test_dietary_vetoes_are_never_negotiated(self):
        ids = [m.candidate_id for m in near_misses(self.replies, limit=50)]
        self.assertNotIn('thai-orchid@2026-10-23T19:00', ids)  # Sam is also unavailable, but diet is reported first
        c = SimpleNamespace(**{**VENUES[3], 'id': 'b'}, slot='2026-10-23T19:00')  # Burger Barn, not vegetarian
        self.assertIsNone(minimal_relaxation(PERSONAS['maya'], c))

    def test_when_formats_slots(self):
        self.assertEqual(negotiation.when('2026-10-25T18:30'), 'Oct 25 at 6:30pm')
        self.assertEqual(negotiation.when('2026-10-25T00:00'), 'Oct 25 at 12am')


class RelaxationTests(unittest.TestCase):
    def test_sam_needs_only_the_missing_slot(self):
        c = SimpleNamespace(**{**VENUES[0], 'id': 'm'}, slot='2026-10-23T19:00')
        changes = minimal_relaxation(PERSONAS['sam'], c)
        self.assertEqual(changes, {'free_slots': PERSONAS['sam']['hard']['free_slots'] + ['2026-10-23T19:00']})
        self.assertEqual(describe_relaxation(PERSONAS['sam'], changes), 'add Oct 23 at 7pm to your free times')

    def test_budget_relaxation_is_exact(self):
        c = SimpleNamespace(**{**VENUES[0], 'id': 'm', 'price': 34}, slot='2026-10-24T19:00')
        changes = minimal_relaxation(PERSONAS['sam'], c)
        self.assertEqual(changes, {'max_price': 34})
        self.assertEqual(describe_relaxation(PERSONAS['sam'], changes), '+$4 budget (to $34)')

    def test_feasible_needs_nothing(self):
        c = SimpleNamespace(**{**VENUES[0], 'id': 'm'}, slot='2026-10-24T19:00')
        self.assertEqual(minimal_relaxation(PERSONAS['sam'], c), {})

    def test_relaxation_unlocks_and_is_minimal(self):
        rng = random.Random(11)
        slots = ['2026-10-23T19:00', '2026-10-24T19:00', '2026-10-25T18:00']
        for _ in range(500):
            p = copy.deepcopy(rng.choice(list(PERSONAS.values())))
            p['hard']['never_cuisines'] = rng.sample(['steak', 'thai', 'burger'], rng.randint(0, 2))
            c = SimpleNamespace(id='x', name='X', cuisine=rng.choice(['steak', 'thai', 'burger', 'fusion']),
                                price=rng.randint(5, 60), distance_min=rng.randint(1, 50), rating=4.0,
                                tags=rng.sample(['vegetarian', 'halal'], rng.randint(0, 2)), slot=rng.choice(slots))
            changes = minimal_relaxation(p, c)
            if changes is None:
                self.assertTrue(evaluate(p, c)[0])
                continue
            relaxed = validate_update(p, changes) if changes else p
            self.assertFalse(evaluate(relaxed, c)[0])
            if 'max_price' in changes:
                self.assertTrue(evaluate(validate_update(p, {**changes, 'max_price': changes['max_price'] - 1}), c)[0])
            if 'max_distance_min' in changes and changes['max_distance_min'] > 1:
                self.assertTrue(evaluate(validate_update(p, {**changes, 'max_distance_min': changes['max_distance_min'] - 1}), c)[0])


class CompromiseTests(unittest.TestCase):
    TABLE = {'A': {'a': .9, 'b': .85, 'c': .3}, 'B': {'a': .6, 'b': .6, 'c': .6},
             'C': {'a': .5, 'b': .5, 'c': .5}, 'D': {'a': .95, 'b': .2, 'c': .9}}

    def test_pareto_frontier_drops_dominated(self):
        self.assertEqual(pareto_frontier(self.TABLE), ['A', 'B', 'D'])

    def test_compromise_set_lines(self):
        options = compromise_set(self.TABLE, names={'A': 'Thai', 'B': 'Med', 'D': 'Steak'})
        self.assertEqual([o.line for o in options], ['A: Med: everyone ≥ 0.60 (fair pick)',
                                                     'B: Thai: 2 people love it, lowest is 0.30',
                                                     'C: Steak: 2 people love it, lowest is 0.20'])
        self.assertEqual(len(compromise_set(self.TABLE, k=2)), 2)

    def test_propose_only_when_someone_is_unhappy(self):
        self.assertIsNone(propose_compromise({'A': {'a': .8, 'b': .7}}))
        p = propose_compromise({'A': {'a': .9, 'b': .3}, 'B': {'a': .35, 'b': .95}})
        self.assertEqual(len(p.options), 2)
        self.assertGreater(max(p.rotation_credits.values()), 0)  # whoever compromised banks credit


if __name__ == '__main__':
    unittest.main()
