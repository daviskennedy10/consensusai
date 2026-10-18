import copy
import json
import os
import unittest
import urllib.error
from pathlib import Path

from nl_preferences import propose
from preferences import validate_update

BASE = Path(__file__).resolve().parent
PERSONAS = json.loads((BASE/'personas.json').read_text())


def fake(reply):
    calls = []

    def llm(system, user):
        calls.append((system, user))
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)
    llm.calls = calls
    return llm


class NaturalLanguageTests(unittest.TestCase):
    def test_delta_applied_to_current_values(self):
        sam = PERSONAS['sam']  # halal, free 24 + 25, max $30
        llm = fake({'max_price': '35', 'dietary_add': ['Vegetarian'], 'free_slots_remove': ['2026-10-24T19:00'],
                    'comfort_price': 20})
        p = propose("I'm vegetarian now too, can't do Saturday, max $35 but ideally $20", sam, llm)
        self.assertEqual(p.source, 'stepfun')
        self.assertEqual(p.update, {'max_price': 35, 'comfort_price': 20, 'dietary': ['halal', 'vegetarian'],
                                    'free_slots': ['2026-10-25T18:00']})
        self.assertEqual(p.rejected, ())
        self.assertIn('max_price: 30 -> 35', p.summary)
        self.assertIn('free_slots: - Oct 24 at 7pm', p.summary)
        self.assertEqual(validate_update(sam, p.update)['hard']['max_price'], 35)  # confirm would apply cleanly

    def test_only_a_proposal_persona_untouched(self):
        sam = copy.deepcopy(PERSONAS['sam'])
        propose('budget is $100 now', sam, fake({'max_price': 100}))
        self.assertEqual(sam, PERSONAS['sam'])

    def test_bad_fields_rejected_one_by_one(self):
        llm = fake({'max_price': -5, 'free_slots_add': ['2026-12-31T19:00'], 'mood': 'happy',
                    'weights': {'price': 5, 'vibes': 9}, 'cuisine_pref': {'thai': 7}, 'max_distance_min': 25})
        p = propose('whatever', PERSONAS['maya'], llm)
        self.assertEqual(set(p.update), {'max_distance_min', 'weights'})
        self.assertEqual(p.update['weights']['price'], 5)
        text = ' | '.join(p.rejected)
        for bad in ('max_price', 'unknown slot 2026-12-31T19:00', 'mood: unknown field', 'unknown key vibes', 'cuisine_pref'):
            self.assertIn(bad, text)

    def test_weights_merge_on_one_scale(self):
        jordan = PERSONAS['jordan']  # cuisine .6, rating 1, distance .3, price .3
        p = propose('ratings matter way more than price', jordan, fake({'weights': {'rating': 10, 'price': 1}}))
        w = p.update['weights']
        self.assertEqual(w, {'cuisine': 6.0, 'rating': 10, 'distance': 3.0, 'price': 1})
        share = lambda w, k: w[k] / sum(w.values())
        self.assertLess(share(w, 'price'), share(jordan['weights'], 'price'))  # price really matters less now
        self.assertIn('weights.price: 14% -> 5% of the decision', p.summary)

    def test_cannot_remove_every_free_slot(self):
        llm = fake({'free_slots_remove': ['2026-10-24T19:00', '2026-10-25T18:00']})
        p = propose("I can't make any of these", PERSONAS['sam'], llm)
        self.assertEqual(p.update, {})
        self.assertTrue(any(r.startswith('free_slots') for r in p.rejected))

    def test_prompt_never_contains_stored_persona(self):
        p = copy.deepcopy(PERSONAS['maya'])
        p['hard'].update(max_price=87, max_distance_min=41, dietary=['kosher'])
        llm = fake({})
        propose('I love thai food', p, llm)
        prompt = ' '.join(llm.calls[0])
        for secret in ('87', '41', 'kosher'):
            self.assertNotIn(secret, prompt)
        self.assertIn('2026-10-23T19:00 = Friday Oct 23 at 7pm', prompt)  # public slots only

    def test_set_command_skips_model(self):
        p = propose('set max_price 28', PERSONAS['maya'], fake(AssertionError('model must not be called')))
        self.assertEqual((p.source, p.update), ('command', {'max_price': 28.0}))
        bad = propose('set max_price 5000', PERSONAS['maya'], fake(AssertionError('model must not be called')))
        self.assertEqual(bad.update, {})
        self.assertTrue(bad.rejected)

    def test_model_failures_never_crash(self):
        for reply in (urllib.error.URLError('down'), TimeoutError(), 'not json at all', '[1, 2]'):
            p = propose('cheaper please', PERSONAS['maya'], fake(reply))
            self.assertEqual((p.update, p.source), ({}, 'none'))
            self.assertIn('set max_price', p.rejected[0])

    def test_fenced_json_and_noops(self):
        p = propose('thai is my fave', PERSONAS['maya'], fake('```json\n{"cuisine_pref": {"Thai": 1.0}}\n```'))
        self.assertEqual(p.update, {})  # maya already loves thai
        self.assertEqual(p.summary, 'No preference changes found.')
        p = propose('never steak', PERSONAS['maya'], fake('Sure! {"never_cuisines_add": ["Steak"]}'))
        self.assertEqual(p.update, {'never_cuisines': ['steak']})
        p = propose('not into burgers', PERSONAS['maya'], fake({'cuisine_pref': {'Burgers': 0.0}, 'never_cuisines_add': ['steaks']}))
        self.assertEqual(p.update['cuisine_pref']['burger'], 0.0)
        self.assertNotIn('burgers', p.update['cuisine_pref'])
        self.assertEqual(p.update['never_cuisines'], ['steak'])


@unittest.skipUnless(os.environ.get('STEPFUN_LIVE_TEST') and os.environ.get('STEPFUN_API_KEY'),
                     'set STEPFUN_LIVE_TEST=1 and STEPFUN_API_KEY to call the real step-5-preview')
class LiveStepFunTests(unittest.TestCase):
    def test_real_model_extracts_and_validates(self):
        p = propose("I'm vegetarian now, can't do Saturday anymore, and ideally keep it under $20",
                    PERSONAS['sam'])
        self.assertEqual(p.source, 'stepfun', p.rejected)
        self.assertIn('vegetarian', p.update.get('dietary', []))
        self.assertEqual(p.update.get('free_slots'), ['2026-10-25T18:00'])
        self.assertEqual(p.update.get('comfort_price'), 20)
        self.assertNotIn('max_price', p.update)  # "ideally" is soft, not a deal breaker


if __name__ == '__main__':
    unittest.main()
