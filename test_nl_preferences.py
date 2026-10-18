import copy
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import nl_preferences
from nl_preferences import propose, stepfun_chat
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


class StepFunCallTests(unittest.TestCase):
    """The real stepfun_chat against a fake HTTP layer: retries, hints, cut-off answers."""

    def urlopen(self, *results):
        results, calls = list(results), []

        def fake(req, timeout):
            calls.append(json.loads(req.data))
            r = results.pop(0)
            if isinstance(r, int):
                raise urllib.error.HTTPError(req.full_url, r, 'err', {}, io.BytesIO(b''))
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = json.dumps({'choices': [r]}).encode()
            return resp
        fake.calls = calls
        return fake

    def call(self, fake):
        with mock.patch.dict(os.environ, {'STEP_API_KEY': 'test-key'}), \
             mock.patch.object(nl_preferences.urllib.request, 'urlopen', fake):
            return stepfun_chat('sys', 'hi', sleep=lambda s: None)

    def test_request_matches_stepfun_starter(self):
        fake = self.urlopen({'finish_reason': 'stop', 'message': {'content': '{}'}})
        self.assertEqual(self.call(fake), '{}')
        body = fake.calls[0]
        self.assertEqual((body['model'], body['reasoning_effort'], body['max_tokens']), ('step-5-preview', 'low', 4096))
        self.assertEqual(nl_preferences.STEPFUN_URL, 'https://api.stepfun.ai/v1/chat/completions')

    def test_429_and_5xx_retry_with_backoff(self):
        fake = self.urlopen(429, 503, {'finish_reason': 'stop', 'message': {'content': '{"max_price": 30}'}})
        self.assertEqual(self.call(fake), '{"max_price": 30}')
        self.assertEqual(len(fake.calls), 3)

    def test_errors_carry_stepfun_hints(self):
        for code, hint in ((402, 'balance'), (401, 'STEP_API_KEY'), (429, 'rate or quota')):
            with self.assertRaises(nl_preferences.StepFunError) as e:
                self.call(self.urlopen(*([code] * 3)))
            self.assertIn(f'HTTP {code}', str(e.exception))
            self.assertIn(hint, str(e.exception))

    def test_cut_off_answer_is_an_error(self):
        with self.assertRaises(nl_preferences.StepFunError):
            self.call(self.urlopen({'finish_reason': 'length', 'message': {'content': ''}}))

    def test_hint_reaches_the_owner(self):
        def broke(system, user):
            raise nl_preferences.StepFunError('StepFun HTTP 402: check the StepFun balance or voucher')
        p = propose('cheaper', PERSONAS['maya'], broke)
        self.assertIn('balance or voucher', p.rejected[0])

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(nl_preferences.StepFunError):
            stepfun_chat('sys', 'hi')


@unittest.skipUnless(os.environ.get('STEPFUN_LIVE_TEST') and (os.environ.get('STEP_API_KEY') or os.environ.get('STEPFUN_API_KEY')),
                     'set STEPFUN_LIVE_TEST=1 and STEP_API_KEY to call the real step-5-preview')
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
