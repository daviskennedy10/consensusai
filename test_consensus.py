import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from preferences import parse_update, validate_update
from scoring import build_complete_table, choose, evaluate

BASE=Path(__file__).resolve().parent
PERSONAS=json.loads((BASE/'personas.json').read_text())
VENUES=json.loads((BASE/'venues.json').read_text())
class ConsensusTests(unittest.TestCase):
    def test_hard_constraints_stay_hard(self):
        c=SimpleNamespace(**VENUES[2],slot='2026-10-24T19:00')
        self.assertTrue(evaluate(PERSONAS['sam'],c)[0])
    def test_maximin_prefers_fair_option(self):
        table={'A':{'maya':.9,'jordan':.9,'sam':.1},'B':{'maya':.6,'jordan':.6,'sam':.6}}
        self.assertEqual(choose(table),('B','A'))
    def test_missing_agent_never_produces_valid_table(self):
        scores=[SimpleNamespace(candidate_id='a',veto=False,score=.7)]
        replies={'maya':SimpleNamespace(scores=scores),'jordan':SimpleNamespace(scores=scores)}
        self.assertIsNone(build_complete_table(replies,[SimpleNamespace(id='a')],PERSONAS))
    def test_incomplete_scores_never_produce_valid_table(self):
        replies={p:SimpleNamespace(scores=[]) for p in PERSONAS}
        self.assertIsNone(build_complete_table(replies,[SimpleNamespace(id='a')],PERSONAS))
    def test_chat_update_preserves_unchanged_fields(self):
        original=copy.deepcopy(PERSONAS['maya'])
        update=parse_update('set max_price $35')
        result=validate_update(original,update)
        self.assertEqual(result['hard']['max_price'],35)
        self.assertEqual(result['hard']['dietary'],original['hard']['dietary'])
        self.assertEqual(original['hard']['max_price'],25)
    def test_invalid_weights_rejected(self):
        with self.assertRaises(ValueError):
            validate_update(PERSONAS['maya'],{'weights':{'price':0}})
    def test_dietary_updating(self):
        result=validate_update(PERSONAS['maya'],parse_update('set dietary vegetarian,halal'))
        self.assertEqual(result['hard']['dietary'],['halal','vegetarian'])

if __name__=='__main__':unittest.main()
