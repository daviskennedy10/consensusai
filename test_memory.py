"""Opt-in: runs against TEST_DATABASE_URL inside a throwaway schema (dropped after), else skipped.
Tiger: TEST_DATABASE_URL="$DATABASE_URL" python -m unittest test_memory"""
import json
import os
import unittest
import uuid
from pathlib import Path

try:
    import psycopg
    import memory
except ImportError:
    psycopg = None

import fairness

URL = os.environ.get('TEST_DATABASE_URL')


@unittest.skipUnless(psycopg and URL, 'set TEST_DATABASE_URL (and install psycopg) to run group memory tests')
class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_' + uuid.uuid4().hex[:12]
        self.conn = psycopg.connect(URL, autocommit=True)
        self.conn.execute(f'CREATE SCHEMA {self.schema}')
        self.conn.execute(f'SET search_path TO {self.schema}')
        memory.init(self.conn)
        for m in ('maya', 'jordan', 'sam'):
            memory.add_member(self.conn, m, m.title())
        memory.create_group(self.conn, 'g', 'Friday dinner', ['maya', 'jordan', 'sam'])

    def tearDown(self):
        self.conn.execute(f'DROP SCHEMA {self.schema} CASCADE')
        self.conn.close()

    def test_init_is_idempotent_and_group_has_members(self):
        memory.init(self.conn)
        self.assertEqual(memory.group_members(self.conn, 'g'), ['jordan', 'maya', 'sam'])

    def test_likes_round_trip_and_are_validated(self):
        memory.set_likes(self.conn, 'maya', {'Thai': 1.0, 'burger': 0.1})
        self.assertEqual(memory.get_likes(self.conn, 'maya'), {'thai': 1.0, 'burger': 0.1})
        with self.assertRaises(psycopg.errors.CheckViolation):
            memory.set_likes(self.conn, 'maya', {'thai': 1.5})
        self.assertEqual(memory.get_likes(self.conn, 'maya'), {'thai': 1.0, 'burger': 0.1})  # failed write rolled back

    def test_group_consensus_is_maximin_over_shared_cuisines(self):
        memory.set_likes(self.conn, 'maya', {'thai': 1.0, 'fusion': 0.8})
        memory.set_likes(self.conn, 'jordan', {'thai': 0.5, 'fusion': 0.7})
        memory.set_likes(self.conn, 'sam', {'thai': 0.4, 'fusion': 0.5, 'burger': 1.0})
        self.assertEqual(memory.group_consensus(self.conn, 'g'), [('fusion', 0.5), ('thai', 0.4)])

    def test_confirmed_plan_banks_credit_once_and_feeds_decide(self):
        table = {'X': {'maya': .70, 'jordan': .70, 'sam': .60}, 'Y': {'maya': .58, 'jordan': .80, 'sam': .80}}
        d = fairness.decide(table)
        self.assertEqual(d.chosen, 'X')
        memory.record_plan(self.conn, 'p1', 'g', d)
        self.assertEqual(memory.get_credits(self.conn, 'g'), {})  # proposing alone changes nothing
        self.assertTrue(memory.confirm_plan(self.conn, 'p1'))
        self.assertFalse(memory.confirm_plan(self.conn, 'p1'))
        credits = memory.get_credits(self.conn, 'g')
        self.assertEqual(credits, fairness.update_credits({}, d))
        self.assertGreater(credits['sam'], 0)  # sam compromised (Y was better for sam)
        self.assertEqual(credits['maya'], 0)
        next_time = {'X': {'maya': .70, 'jordan': .70, 'sam': .60}, 'Y': {'maya': .58, 'jordan': .75, 'sam': .80}}
        self.assertEqual(fairness.decide(next_time).chosen, 'X')
        self.assertEqual(fairness.decide(next_time, history=credits).chosen, 'Y')  # sam's turn this time

    def test_cancelled_plan_never_moves_credits(self):
        d = fairness.decide({'X': {'maya': .9, 'jordan': .2, 'sam': .5}})
        memory.record_plan(self.conn, 'p2', 'g', d)
        self.assertTrue(memory.cancel_plan(self.conn, 'p2'))
        self.assertFalse(memory.confirm_plan(self.conn, 'p2'))
        self.assertEqual(memory.get_credits(self.conn, 'g'), {})

    def test_forget_member_removes_everything(self):
        memory.set_likes(self.conn, 'sam', {'thai': 0.4})
        d = fairness.decide({'X': {'maya': .9, 'jordan': .2, 'sam': .1}, 'Y': {'maya': .1, 'jordan': .3, 'sam': .9}})
        memory.record_plan(self.conn, 'p3', 'g', d)
        memory.confirm_plan(self.conn, 'p3')
        self.assertTrue(memory.forget_member(self.conn, 'sam'))
        self.assertEqual(memory.get_likes(self.conn, 'sam'), {})
        self.assertNotIn('sam', memory.group_members(self.conn, 'g'))
        self.assertNotIn('sam', memory.get_credits(self.conn, 'g'))

    def test_seed_demo(self):
        memory.seed_demo(self.conn)
        personas = json.loads((Path(__file__).resolve().parent / 'personas.json').read_text())
        self.assertEqual(memory.group_members(self.conn, 'demo'), sorted(personas))
        self.assertEqual(memory.group_consensus(self.conn, 'demo')[0][0], 'fusion')


if __name__ == '__main__':
    unittest.main()
