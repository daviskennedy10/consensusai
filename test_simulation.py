import unittest

from bench_fairness import simulate, summary
from scoring import evaluate


class SimulationTests(unittest.TestCase):
    """Seeded random groups through the real evaluate -> build_complete_table -> decide path."""

    @classmethod
    def setUpClass(cls):
        cls.runs = {t: simulate(300, seed=42, objectives=('leximin', 'maximin', 'nash'), tastes=t)
                    for t in ('random', 'majority')}

    def test_fair_pick_never_worse_for_worst_off(self):
        for rows in self.runs.values():
            for r in rows:
                self.assertGreaterEqual(r['leximin'][0], r['utilitarian'][0])
                self.assertGreaterEqual(r['maximin'][0], r['utilitarian'][0])

    def test_chosen_plans_never_break_hard_constraints(self):
        for rows in self.runs.values():
            for r in rows:
                for cid in set(r['chosen'].values()):
                    for p in r['people'].values():
                        self.assertFalse(evaluate(p, r['cands'][cid])[0])

    def test_fairness_beats_averaging_at_small_cost(self):
        for tastes, rows in self.runs.items():
            choice = [r for r in rows if r['options'] > 1]
            self.assertGreater(len(choice), 100, tastes)
            s = summary(choice, 'leximin')
            self.assertGreaterEqual(s['improved_pct'], 5, tastes)   # strictly better worst-off in a real share
            self.assertGreater(s['worst_gain_all'], 0, tastes)
            self.assertLessEqual(s['mean_cost'], 0.02, tastes)       # welfare cost stays small
            self.assertLessEqual(s['gini'], s['gini_avg'], tastes)


if __name__ == '__main__':
    unittest.main()
