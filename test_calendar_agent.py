"""Exercise the ASI:One handlers with no running agents or external services."""
import importlib.util
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import google_calendar
from models import Candidate, CounterofferRequest, ScoreRequest
from coordination import Plan
from scoring import build_complete_table
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent


async def inline_thread_call(fn, *args, **kwargs):
    # Handler tests exercise authorization and scoring, with no executor or I/O.
    return fn(*args, **kwargs)


class InertAgent:
    address = 'this-agent'

    def __init__(self, *args, **kwargs):
        pass

    def on_message(self, *args, **kwargs):
        return lambda fn: fn

    on_interval = on_message

    def include(self, *args, **kwargs):
        pass


class AgentCalendarTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('calendar_test_agent', Path(__file__).with_name('personal_agent.py'))
        cls.module = importlib.util.module_from_spec(spec)
        with patch('sys.argv', ['personal_agent.py', 'maya']), patch.dict(os.environ, {'DATABASE_URL': ''}), \
                patch('uagents.Agent', InertAgent), patch('uagents.Protocol', InertAgent):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
        self.owner = 'owner-address'
        self.env = patch.dict(os.environ, {'MAYA_OWNER_ADDRESSES': self.owner})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calendar = Mock()
        self.calendar.connected.return_value = True
        self.patches = [patch.object(self.module, 'calendar', self.calendar),
                        patch.object(self.module, 'reply', AsyncMock()),
                        patch.object(self.module.asyncio, 'to_thread', inline_thread_call)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.candidate = Candidate(id='test', name='Dinner', cuisine='thai', price=20,
                                   distance_min=10, rating=4.5, tags=['vegetarian'], slot='2026-10-24T19:00')
        self.peer = self.module.ADDR['sam']

    async def chat(self, sender, text):
        await self.module.on_chat(self.ctx, sender, ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type='text', text=text)]))

    async def test_stranger_cannot_revoke_owner_calendar(self):
        await self.chat('stranger', 'calendar disconnect')
        self.calendar.disconnect.assert_not_called()

    async def test_owner_can_inspect_and_disconnect(self):
        await self.chat(self.owner, 'calendar status')
        self.assertIn('connected locally', self.module.reply.call_args.args[2])
        await self.chat(self.owner, 'calendar disconnect')
        self.calendar.disconnect.assert_called_once_with('maya')

    async def test_calendar_failure_returns_incomplete_reply(self):
        with patch.object(google_calendar, 'evaluate_candidates', side_effect=google_calendar.CalendarError('Unavailable')):
            await self.module.on_score(self.ctx, self.peer, ScoreRequest(plan_id='p', round=1, candidates=[self.candidate]))
        reply = self.ctx.send.call_args.args[1]
        self.assertEqual(reply.scores, [])
        self.assertIsNone(build_complete_table({'maya': reply}, [self.candidate], ['maya']))

    async def test_unauthorized_scoring_never_reads_calendar(self):
        with patch.object(google_calendar, 'evaluate_candidates') as evaluate:
            await self.module.on_score(self.ctx, 'stranger', ScoreRequest(plan_id='p', round=1, candidates=[self.candidate]))
        evaluate.assert_not_called()
        self.ctx.send.assert_not_called()

    async def test_coordinator_checks_its_own_calendar_before_asking_peers(self):
        plan = Plan(user=self.owner, participants={'maya': 'this-agent', 'sam': self.peer}, coordinator='this-agent')
        plan.live = [self.candidate]
        with patch.object(google_calendar, 'evaluate_candidates', return_value=[(True, 0.0, 'not free at that time')]) as evaluate:
            await self.module.begin_round(self.ctx, plan)
        evaluate.assert_called_once()
        self.assertTrue(plan.replies['maya'].scores[0].veto)
        self.assertFalse(plan.finalizing)
        self.assertEqual(self.ctx.send.call_args.args[0], self.peer)

    async def test_busy_or_unverified_calendar_cannot_be_relaxed(self):
        for result in ({self.candidate.slot}, google_calendar.CalendarError('Unavailable')):
            with self.subTest(result=result):
                self.calendar.unavailable_slots.side_effect = result if isinstance(result, Exception) else None
                self.calendar.unavailable_slots.return_value = result
                with patch.object(self.module, 'minimal_relaxation') as relax:
                    await self.module.on_counteroffer_request(self.ctx, self.peer, CounterofferRequest(
                        plan_id='p', candidate=self.candidate, category='availability'))
                relax.assert_not_called()
                self.assertEqual(self.ctx.send.call_args.args[1].status, 'not_possible')


if __name__ == '__main__':
    unittest.main()
