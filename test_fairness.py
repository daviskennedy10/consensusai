import random
import unittest
from statistics import mean

import fairness
from fairness import decide, update_credits
from scoring import choose


def random_table(rng, people, candidates):
    names = [f'p{i}' for i in range(people)]
    return {f'c{j}': {p: round(rng.random(), 3) for p in names} for j in range(candidates)}


class ObjectiveTests(unittest.TestCase):
    def test_utilitarian_maximizes_mean(self):
        t = {'A': {'a': .9, 'b': .9, 'c': .1}, 'B': {'a': .6, 'b': .6, 'c': .6}}
        self.assertEqual(decide(t, 'utilitarian').chosen, 'A')

    def test_maximin_and_leximin_protect_worst_off(self):
        t = {'A': {'a': .9, 'b': .9, 'c': .1}, 'B': {'a': .6, 'b': .6, 'c': .6}}
        self.assertEqual(decide(t, 'maximin').chosen, 'B')
        self.assertEqual(decide(t).chosen, 'B')

    def test_leximin_breaks_maximin_ties_by_second_worst(self):
        t = {'A': {'a': .5, 'b': .5, 'c': .9}, 'B': {'a': .5, 'b': .6, 'c': .6}}
        self.assertEqual(decide(t, 'maximin').chosen, 'A')  # equal min, higher mean
        self.assertEqual(decide(t, 'leximin').chosen, 'B')  # second-worst person is better off

    def test_nash_balances_efficiency_and_fairness(self):
        t = {'A': {'a': 1.0, 'b': 1.0, 'c': .01}, 'B': {'a': .5, 'b': .5, 'c': .5}}
        self.assertEqual(decide(t, 'nash').chosen, 'B')
        t = {'A': {'a': .3, 'b': .9, 'c': .9}, 'B': {'a': .35, 'b': .4, 'c': .4}}
        self.assertEqual(decide(t, 'maximin').chosen, 'B')
        self.assertEqual(decide(t, 'nash').chosen, 'A')  # tiny min loss, big gain for others

    def test_unknown_objective_and_empty_table(self):
        with self.assertRaises(ValueError):
            decide({'A': {'a': .5}}, 'random')
        with self.assertRaises(ValueError):
            decide({})

    def test_tie_breaking_is_deterministic(self):
        rows = {'a': .5, 'b': .5}
        for objective in fairness.OBJECTIVES:
            self.assertEqual(decide({'x': dict(rows), 'y': dict(rows)}, objective).chosen, 'y')
            self.assertEqual(decide({'y': dict(rows), 'x': dict(rows)}, objective).chosen, 'y')

    def test_group_sizes_two_to_ten(self):
        rng = random.Random(3)
        for people in range(2, 11):
            for _ in range(50):
                t = random_table(rng, people, rng.randint(1, 8))
                d = decide(t)
                self.assertEqual(min(t[d.chosen].values()), max(min(r.values()) for r in t.values()))
                self.assertEqual(set(d.metrics['regret']), set(t[d.chosen]))
                self.assertEqual(sorted(d.ranking), sorted(t))
                self.assertEqual(d.average_id, max(t, key=lambda c: (mean(t[c].values()), c)))


class DecisionTests(unittest.TestCase):
    def test_metrics(self):
        t = {'A': {'a': .9, 'b': .9, 'c': .1}, 'B': {'a': .6, 'b': .6, 'c': .6}}
        m = decide(t).metrics
        self.assertEqual((m['min'], m['mean'], m['spread'], m['gini']), (.6, .6, 0.0, 0.0))
        self.assertEqual(m['regret'], {'a': .3, 'b': .3, 'c': 0.0})
        self.assertGreater(decide(t, 'utilitarian').metrics['gini'], 0.2)

    def test_explanation_compares_with_average_pick(self):
        t = {'A': {'a': .9, 'b': .9, 'c': .18}, 'B': {'a': .62, 'b': .7, 'c': .65}}
        d = decide(t, names={'A': 'Burger Barn', 'B': 'Garden Fusion'})
        self.assertEqual(d.explanation, 'Garden Fusion keeps everyone at 0.62 or higher; the highest-average '
                                        'option, Burger Barn, would leave someone at 0.18.')
        self.assertIn('also has the highest average', decide({'A': {'a': .5}}).explanation)

    def test_choose_keeps_legacy_contract(self):
        rng = random.Random(5)
        for _ in range(300):
            t = random_table(rng, rng.randint(2, 6), rng.randint(1, 6))
            fair, avg = choose(t)
            legacy_fair = max(t, key=lambda k: (min(t[k].values()), mean(t[k].values()), k))
            legacy_avg = max(t, key=lambda k: (mean(t[k].values()), k))
            self.assertEqual(min(t[fair].values()), min(t[legacy_fair].values()))
            self.assertEqual(avg, legacy_avg)


class CreditTests(unittest.TestCase):
    def test_credit_breaks_near_ties_toward_past_loser(self):
        t = {'X': {'a': .70, 'b': .60}, 'Y': {'a': .58, 'b': .80}}
        self.assertEqual(decide(t).chosen, 'X')
        d = decide(t, history={'b': .1})
        self.assertEqual(d.chosen, 'Y')  # b lost last time and is much happier at Y
        self.assertIn('Fairness credit', d.explanation)
        self.assertEqual(d.metrics['min'], .58)  # metrics use raw scores
        self.assertEqual(decide(t, 'nash', history={'b': .1}).chosen, 'Y')

    def test_credit_is_capped(self):
        t = {'A': {'a': .9, 'b': .3}, 'B': {'a': .5, 'b': .6}}
        self.assertEqual(decide(t, history={'a': 99}).chosen, 'B')  # cap stops a credit hoarder overturning it

    def test_update_credits_rewards_compromise_and_stays_bounded(self):
        t = {'A': {'a': .9, 'b': .2}, 'B': {'a': .5, 'b': .5}}
        history = {}
        for _ in range(20):
            history = update_credits(history, decide(t, history=history))
            self.assertTrue(all(0 <= v <= fairness.CREDIT_CAP for v in history.values()))
        self.assertGreater(history['a'], history['b'])  # a compromised every time


if __name__ == '__main__':
    unittest.main()
