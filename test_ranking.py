import copy
import json
import random
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import constraints
import ranking
from preferences import load_persona, parse_update, save_persona, validate_update
from scoring import evaluate

BASE = Path(__file__).resolve().parent
PERSONAS = json.loads((BASE/'personas.json').read_text())


def cand(**kw):
    base = dict(id='x@2026-10-24T19:00', name='X', cuisine='thai', price=20, distance_min=10,
                rating=4.5, tags=['vegetarian', 'halal'], slot='2026-10-24T19:00')
    return SimpleNamespace(**{**base, **kw})


def persona(**soft):
    p = copy.deepcopy(PERSONAS['maya'])
    if soft:
        p['soft'] = soft
    return p


class RankingTests(unittest.TestCase):
    def test_score_in_unit_range_and_parts_sum(self):
        s, parts = ranking.score(persona(), cand())
        self.assertTrue(0 <= s <= 1)
        self.assertAlmostEqual(s, sum(parts.values()))
        self.assertNotIn('time', parts)  # no preferred slots -> feature does not apply

    def test_weights_are_normalized(self):
        p = persona()
        p2 = copy.deepcopy(p)
        p2['weights'] = {k: v * 10 for k, v in p['weights'].items()}
        self.assertAlmostEqual(ranking.score(p, cand())[0], ranking.score(p2, cand())[0])

    def test_legacy_linear_decay_without_comfort(self):
        self.assertAlmostEqual(ranking.threshold_utility(10, 20), 0.5)
        self.assertEqual(ranking.threshold_utility(20, 20), 0.0)

    def test_comfort_level_is_flat_then_smooth_to_floor(self):
        u = lambda x: ranking.threshold_utility(x, 30, comfort=20)
        self.assertEqual(u(15), 1.0)
        self.assertEqual(u(20), 1.0)
        self.assertAlmostEqual(u(30), ranking.COMFORT_FLOOR)
        vals = [u(x) for x in range(20, 31)]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertTrue(all(ranking.COMFORT_FLOOR <= v <= 1 for v in vals))

    def test_comfort_price_raises_score_within_comfort(self):
        c = cand(price=20)
        self.assertGreater(ranking.score(persona(comfort_price=20), c)[0], ranking.score(persona(), c)[0])

    def test_rating_spread_rewards_good_range(self):
        u = lambda r: ranking.rating_utility(None, cand(rating=r))
        self.assertGreater(u(4.9) - u(4.1), u(1.8) - u(1.0))
        self.assertEqual(u(2.0), 0.0)
        self.assertEqual(u(5.0), 1.0)

    def test_unknown_cuisine_uses_configurable_neutral(self):
        p = persona()
        self.assertEqual(ranking.cuisine_utility(p, cand(cuisine='ethiopian')), ranking.NEUTRAL_CUISINE)
        self.assertEqual(ranking.cuisine_utility(p, cand(cuisine='ethiopian'), neutral=0.5), 0.5)
        self.assertEqual(ranking.cuisine_utility(p, cand(cuisine='THAI')), 1.0)

    def test_preferred_slot_is_soft(self):
        p = persona(preferred_slots=['2026-10-24T19:00'])
        hit, miss = cand(), cand(slot='2026-10-23T19:00')
        self.assertGreater(evaluate(p, hit)[1], evaluate(p, miss)[1])
        self.assertFalse(evaluate(p, miss)[0])  # not preferred is never a veto

    def test_features_are_pluggable(self):
        genre = ranking.Feature('genre', lambda p, c: p['genre_pref'].get(c.genre, 0.0), default_weight=1.0)
        features = {**ranking.RESTAURANT_FEATURES, 'genre': genre}
        p = {**persona(), 'genre_pref': {'jazz': 1.0}}
        jazz, rock = cand(genre='jazz'), cand(genre='rock')
        self.assertGreater(ranking.score(p, jazz, features)[0], ranking.score(p, rock, features)[0])
        self.assertIn('genre', ranking.score(p, jazz, features)[1])

    def test_reason_names_top_driver(self):
        p = persona()
        p['weights'] = {'cuisine': 0, 'rating': 0, 'distance': 0, 'price': 1}
        self.assertTrue(evaluate(p, cand())[2].endswith('driven by price'))


class ConstraintTests(unittest.TestCase):
    def test_constraints_are_declarative(self):
        self.assertEqual({k.name for k in constraints.HARD},
                         {'free_slots', 'max_price', 'max_distance_min', 'dietary', 'never_cuisines'})
        self.assertTrue(all(k.kind == 'hard' for k in constraints.HARD))
        self.assertTrue(all(k.kind == 'soft' for k in constraints.SOFT))

    def test_each_hard_constraint_vetoes(self):
        p = persona()
        p['hard']['never_cuisines'] = ['burger']
        cases = {'availability': cand(slot='2026-10-26T19:00'), 'budget': cand(price=26),
                 'distance': cand(distance_min=21), 'dietary': cand(tags=['halal']),
                 'cuisine': cand(cuisine='burger')}
        for category, c in cases.items():
            veto, score, reason = evaluate(p, c)
            self.assertTrue(veto, category)
            self.assertEqual(score, 0.0)
            self.assertEqual(constraints.category_for_reason(reason), category)

    def test_comfort_never_relaxes_hard_max(self):
        p = persona(comfort_price=100, comfort_distance_min=100)
        self.assertTrue(evaluate(p, cand(price=26))[0])
        self.assertTrue(evaluate(p, cand(distance_min=21))[0])

    def test_reasons_never_leak_limits_or_diet(self):
        rng = random.Random(7)
        diets = ['vegetarian', 'halal', 'vegan', 'kosher', 'gluten-free']
        for _ in range(500):
            p = {'hard': {'max_price': rng.randint(10, 60), 'max_distance_min': rng.randint(5, 40),
                          'dietary': rng.sample(diets, rng.randint(0, 2)),
                          'free_slots': ['2026-10-24T19:00'], 'never_cuisines': ['steak']},
                 'cuisine_pref': {}, 'weights': {'cuisine': 1, 'rating': 1, 'distance': 1, 'price': 1}}
            c = cand(price=rng.randint(5, 80), distance_min=rng.randint(1, 60), cuisine=rng.choice(['thai', 'steak']),
                     tags=rng.sample(diets, rng.randint(0, 3)), slot=rng.choice(['2026-10-24T19:00', '2026-10-23T19:00']))
            veto, _, reason = evaluate(p, c)
            if veto:
                self.assertIsNone(re.search(r'\d', reason), reason)
                self.assertFalse(any(d in reason for d in diets), reason)
                self.assertNotEqual(constraints.category_for_reason(reason), 'other')


class PreferenceFieldTests(unittest.TestCase):
    def test_new_fields_parse_and_validate(self):
        p = PERSONAS['sam']
        r = validate_update(p, parse_update('set comfort_price $20'))
        self.assertEqual(r['soft']['comfort_price'], 20)
        self.assertNotIn('soft', p)
        r = validate_update(p, parse_update('set never_cuisines Steak, thai'))
        self.assertEqual(r['hard']['never_cuisines'], ['steak', 'thai'])
        r = validate_update(p, parse_update('set preferred_slots 2026-10-24T19:00'))
        self.assertEqual(r['soft']['preferred_slots'], ['2026-10-24T19:00'])
        r = validate_update(p, {'weights': {'cuisine': 1, 'rating': 1, 'distance': 1, 'price': 1, 'time': 2}})
        self.assertEqual(r['weights']['time'], 2)
        with self.assertRaises(ValueError):
            validate_update(p, {'weights': {'cuisine': 1, 'rating': 1, 'distance': 1, 'price': 1, 'mood': 2}})
        with self.assertRaises(ValueError):
            validate_update(p, {'comfort_price': -1})

    def test_new_fields_survive_save_and_load(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(BASE/'personas.json', tmp/'personas.json')
        p = validate_update(PERSONAS['maya'], {'comfort_price': 18, 'never_cuisines': ['steak'],
                                               'preferred_slots': ['2026-10-24T19:00']})
        save_persona('maya', tmp, p)
        loaded = load_persona('maya', tmp)
        self.assertEqual(loaded['soft'], p['soft'])
        self.assertEqual(loaded['hard']['never_cuisines'], ['steak'])


if __name__ == '__main__':
    unittest.main()
