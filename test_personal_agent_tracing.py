"""Offline handler tests: no real uAgent, identity, database, browser or persona IO.

The actual personal_agent.py module is executed with fake runtime imports installed
BEFORE its startup code runs. Domain scoring/negotiation functions remain real.
Run explicitly with test_activity_trace; do not use integration-test discovery.
"""
import copy
import asyncio
from contextlib import ExitStack
from enum import Enum
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from activity_trace import ActivityTrace, EventType as E
import preferences


ROOT = Path(__file__).resolve().parent
SLOT = "2026-10-24T19:00"
PUBLIC_NAMES = ("alex", "maya", "jordan", "sam")


class DeliveryStatus(str, Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


class FakeModel(SimpleNamespace):
    pass


class FakeChatMessage(FakeModel):
    @staticmethod
    def build_schema_digest(model):
        assert model is FakeChatMessage
        return "chat-message-schema"

    def model_dump_json(self):
        return json.dumps({"msg_id": str(self.msg_id), "content": [vars(c) for c in self.content]})

    def text(self):
        return "".join(getattr(item, "text", "") for item in self.content)  # like uagents: skips metadata (cards)


class FakeProtocol:
    def __init__(self, **kwargs):
        pass

    def on_message(self, *args, **kwargs):
        return lambda fn: fn


class FakeAgent(FakeProtocol):
    def __init__(self, **kwargs):
        self.address = "agent-alex"

    def on_interval(self, *args, **kwargs):
        return lambda fn: fn

    def include(self, *args, **kwargs):
        pass

    def run(self):
        raise AssertionError("An agent must never be launched by these tests")


class FakeContext:
    def __init__(self, session="current-chat"):
        self._session = session
        self.logger = Mock()
        self.storage = {}
        self.sent = []
        self.send = AsyncMock(side_effect=self._send)
        self.raw_sent = []
        self.send_raw = AsyncMock(side_effect=self._send_raw)

    async def _send_raw(self, **kwargs):
        self.raw_sent.append((kwargs, self.session))
        return SimpleNamespace(status=DeliveryStatus.DELIVERED)

    @property
    def session(self):
        return self._session

    async def _send(self, target, message):
        self.sent.append((target, message, self.session))
        return SimpleNamespace(status=DeliveryStatus.DELIVERED)

    def texts(self):
        return [message.text() for _, message, _ in self.sent
                if isinstance(message, FakeChatMessage)]


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class PersonalAgentTracingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Fail closed if a future handler accidentally reaches a real network path.
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "urllib.request.urlopen"):
            stack.enter_context(patch(target, side_effect=AssertionError("Network forbidden")))
        stack.enter_context(patch.dict(os.environ, {"ALEX_OWNER_ADDRESSES": "owner-a,owner-b"}, clear=True))
        stack.enter_context(patch.object(sys, "argv", ["personal_agent.py", "alex"]))

        base = {"hard": {"max_price": 100, "max_distance_min": 100, "dietary": [],
                         "free_slots": ["2026-10-23T19:00", SLOT, "2026-10-25T18:00"]},
                "weights": {"cuisine": 1, "rating": 1, "distance": 1, "price": 1},
                "cuisine_pref": {"thai": 1}, "port": 0}
        fixtures = {name: {**copy.deepcopy(base), "seed": name} for name in PUBLIC_NAMES}
        venue = {"id": "fixture", "name": "Fixture Venue", "cuisine": "thai",
                 "price": 20, "distance_min": 10, "rating": 4.5, "tags": []}
        stack.enter_context(patch.object(preferences, "load_persona", return_value=copy.deepcopy(fixtures["alex"])))
        self.save_persona = stack.enter_context(patch.object(preferences, "save_persona"))

        self.memory = module("memory", **{name: Mock() for name in
            ("connect", "init", "group_exists", "seed_demo", "get_persona", "save_persona", "record_plan")})
        self.memory.connect.side_effect = AssertionError("Database forbidden")
        self.nl = module("nl_preferences", propose=Mock(side_effect=AssertionError("Unmocked LLM")))
        self.places = module("venues", search=Mock(side_effect=AssertionError("Unmocked Places")))
        self.booking = module("booking", check_availability=Mock(side_effect=AssertionError("Unmocked browser")),
                              message=Mock(return_value="Availability only; not booked"))
        fake_modules = {
            "uagents": module("uagents", Agent=FakeAgent, Context=FakeContext,
                              Protocol=FakeProtocol, Model=FakeModel),
            "uagents.crypto": module("uagents.crypto", Identity=SimpleNamespace(
                from_seed=lambda seed, index: SimpleNamespace(address=f"agent-{seed}"))),
            "uagents_core": module("uagents_core"),
            "uagents_core.types": module("uagents_core.types", DeliveryStatus=DeliveryStatus),
            "uagents_core.contrib": module("uagents_core.contrib"),
            "uagents_core.contrib.protocols": module("uagents_core.contrib.protocols"),
            "uagents_core.contrib.protocols.chat": module(
                "uagents_core.contrib.protocols.chat", ChatMessage=FakeChatMessage,
                ChatAcknowledgement=FakeModel, TextContent=FakeModel, MetadataContent=FakeModel,
                chat_protocol_spec=SimpleNamespace(digest="chat-protocol-digest")),
            "memory": self.memory, "nl_preferences": self.nl,
            "venues": self.places, "booking": self.booking,
        }
        stack.enter_context(patch.dict(sys.modules, fake_modules))
        # Force these repository modules to bind to our fake Model, even if another
        # test imported them earlier. patch.dict restores the original module map.
        for name in ("models", "coordination"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("offline_personal_agent", ROOT / "personal_agent.py")
        self.app = importlib.util.module_from_spec(spec)

        def fixture_text(path, *args, **kwargs):
            if path.name == "personas.json":
                return json.dumps(fixtures)
            if path.name == "venues.json":
                return json.dumps([venue])
            raise AssertionError("Unexpected data-file read")

        with patch.object(Path, "read_text", fixture_text):
            spec.loader.exec_module(self.app)
        self.memory.connect.assert_not_called()
        self.ctx = FakeContext()
        self.candidate = self.app.Candidate(id=f"live@{SLOT}", name="Live Venue", cuisine="thai",
            price=20, distance_min=10, rating=4.5, tags=[], slot=SLOT)

    async def chat(self, text, sender="owner-a", ctx=None):
        await self.app.on_chat(ctx or self.ctx, sender, FakeChatMessage(
            msg_id="synthetic-message", content=[FakeModel(text=text)]))

    def plan(self, round=1):
        p = self.app.Plan(user="owner-a", participants=self.app.ADDR.copy(), coordinator=self.app.agent.address)
        p.session, p.live, p.round = "original-plan-chat", [self.candidate], round
        p.begin_round([self.candidate])
        self.app.plans[p.id] = p
        self.app.activity_traces[p.id] = ActivityTrace(p.id)
        self.app.latest_trace_id[p.user] = p.id
        return p

    def reply_for(self, p, person, veto=False, **changes):
        data = dict(plan_id=p.id, round=p.round, persona=person,
                    scores=[self.app.CandidateScore(candidate_id=self.candidate.id,
                        veto=veto, score=0 if veto else 0.8, reason="over budget" if veto else "private sentinel")])
        data.update(changes)
        return self.app.ScoreReply(**data)

    def fill(self, p, blocker=None):
        p.replies = {person: self.reply_for(p, person, veto=person == blocker)
                     for person in p.participants}

    def events(self, p):
        return self.app.activity_traces[p.id].events

    def kinds(self, p):
        return [e.kind for e in self.events(p)]

    def negotiation(self, p, status="requested"):
        p.finalizing = True
        self.app.negotiations[p.id] = {"person": "maya", "candidate_id": self.candidate.id,
            "started": self.app.time.monotonic(), "status": status}

    async def review(self, p, status, sender="agent-maya", **changes):
        data = dict(plan_id=p.id, persona="maya", candidate_id=self.candidate.id, status=status)
        data.update(changes)
        await self.app.on_counteroffer_reply(self.ctx, sender, self.app.CounterofferReply(**data))

    async def test_plan_registration_search_order_and_duplicate(self):
        async def search(ctx):
            self.assertEqual(self.app.plans, {})
            self.assertEqual(self.app.activity_traces, {})
            return [self.candidate], "Live search note"
        with patch.object(self.app, "find_live_candidates", AsyncMock(side_effect=search)) as search_mock:
            await self.chat("PLAN")
            p = next(iter(self.app.plans.values()))
            self.assertEqual(p.live, [self.candidate])
            self.assertEqual(p.session, "current-chat")
            self.assertEqual(self.kinds(p), [E.PLAN_STARTED, E.SCORE_RECEIVED] + [E.SCORE_REQUESTED] * 3)
            await self.chat("plan")
            search_mock.assert_awaited_once()
            self.assertEqual(len(self.app.activity_traces), 1)

    async def test_trace_authorization_owner_scoping_and_current_session(self):
        p = self.plan()
        self.app.trace_event(p.id, E.PLAN_STARTED, "alex")
        for sender in ("stranger", "owner-b"):
            await self.chat("TRACE", sender)
            self.assertNotIn(p.id, self.ctx.texts()[-1])
        await self.chat("TRACE")
        self.assertIn(p.id, self.ctx.texts()[-1])
        self.assertEqual(self.ctx.session, "current-chat")
        self.nl.propose.assert_not_called()
        self.assertEqual(self.ctx.storage, {})

    async def test_unauthorized_plan_has_no_trace_or_search(self):
        await self.chat("PLAN", "stranger")
        self.assertEqual(self.app.activity_traces, {})
        self.places.search.assert_not_called()

    async def test_exact_trace_preserves_free_text_fallback(self):
        self.nl.propose.side_effect = None
        self.nl.propose.return_value = SimpleNamespace(update={"max_price": 50}, summary="proposed", rejected=())
        self.ctx.storage = SimpleNamespace(get=Mock(), set=Mock())
        await self.chat("trace my preferences")
        self.nl.propose.assert_called_once()
        self.ctx.storage.set.assert_called_once()
        self.save_persona.assert_not_called()

    async def test_scoring_dispatch_requires_explicit_success(self):
        for outcome in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, "unknown", RuntimeError()):
            with self.subTest(outcome=type(outcome).__name__ if isinstance(outcome, Exception) else outcome):
                p = self.plan()
                self.ctx.send.side_effect = outcome if isinstance(outcome, Exception) else None
                self.ctx.send.return_value = SimpleNamespace(status=outcome)
                await self.app.begin_round(self.ctx, p)
                expected = 3 if outcome in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED) else 0
                self.assertEqual(self.kinds(p).count(E.SCORE_REQUESTED), expected)
                self.assertEqual(self.kinds(p)[0], E.SCORE_RECEIVED)

    async def test_counteroffer_dispatch_requires_explicit_success(self):
        for outcome in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, RuntimeError()):
            with self.subTest(outcome=type(outcome).__name__ if isinstance(outcome, Exception) else outcome):
                p = self.plan(round=2)
                self.fill(p, blocker="maya")
                async def send(target, message):
                    if isinstance(message, self.app.CounterofferRequest):
                        if isinstance(outcome, Exception):
                            raise outcome
                        return SimpleNamespace(status=outcome)
                    return SimpleNamespace(status=DeliveryStatus.DELIVERED)
                self.ctx.send.side_effect = send
                await self.app.finalize_peer_plan(self.ctx, p)
                expected = int(outcome in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED))
                self.assertEqual(self.kinds(p).count(E.COUNTEROFFER_SENT), expected)
                self.assertEqual(self.kinds(p)[0], E.NO_FEASIBLE_PLAN)

    async def test_remote_score_guards_and_privacy(self):
        p = self.plan()
        cases = [("agent-maya", {"plan_id": "unknown"}),
                 ("agent-maya", {"round": 2}), ("stranger", {}),
                 ("agent-jordan", {}), ("agent-maya", {"persona": "sam"})]
        for sender, changes in cases:
            await self.app.on_peer_score(self.ctx, sender, self.reply_for(p, "maya", **changes))
        self.assertEqual(self.events(p), [])
        p.finalizing = True
        await self.app.on_peer_score(self.ctx, "agent-maya", self.reply_for(p, "maya"))
        self.assertEqual(self.events(p), [])
        p.finalizing = False
        await self.app.on_peer_score(self.ctx, "agent-maya", self.reply_for(p, "maya"))
        await self.app.on_peer_score(self.ctx, "agent-maya", self.reply_for(p, "maya"))
        self.assertEqual(self.kinds(p), [E.SCORE_RECEIVED])
        rendered = self.app.activity_traces[p.id].render()
        for private in ("private sentinel", "0.8", "owner-a", "agent-maya", "max_price"):
            self.assertNotIn(private, rendered)

    async def test_round_expansion_and_approved_rescoring_order(self):
        p = self.plan()
        self.fill(p, blocker="maya")
        await self.app.finalize_peer_plan(self.ctx, p)
        self.assertEqual(p.round, 2)
        self.assertEqual(self.kinds(p)[:3], [E.NO_FEASIBLE_PLAN, E.ROUND_RETRY, E.SCORE_RECEIVED])
        self.fill(p, blocker="maya")
        await self.app.finalize_peer_plan(self.ctx, p)
        await self.review(p, "approval_required")
        await self.review(p, "approved")
        self.assertEqual(self.kinds(p)[-5:], [E.RESCORING_STARTED, E.SCORE_RECEIVED] + [E.SCORE_REQUESTED] * 3)
        self.assertEqual(self.kinds(p).count(E.ROUND_RETRY), 1)
        self.assertLess(self.kinds(p).index(E.APPROVED), self.kinds(p).index(E.RESCORING_STARTED))
        self.assertEqual(set(p.replies), {"alex"})
        self.assertEqual(p.round, 2)
        self.assertEqual(p.candidates, [self.candidate])
        self.assertFalse(p.finalizing)
        self.assertFalse(p.expired(self.app.TIMEOUT))
        self.places.search.assert_not_called()

    async def test_already_feasible_rescores_without_human_approval(self):
        p = self.plan(round=2)
        self.fill(p)
        self.negotiation(p)
        await self.review(p, "already_feasible")
        self.assertNotIn(E.APPROVED, self.kinds(p))
        self.assertEqual(self.kinds(p)[0], E.RESCORING_STARTED)
        count = len(self.events(p))
        await self.review(p, "already_feasible")
        self.assertEqual(len(self.events(p)), count)

    async def test_review_authentication_and_duplicate_status_guards(self):
        p = self.plan(round=2)
        self.negotiation(p)
        for sender, changes in (("stranger", {}), ("agent-maya", {"persona": "sam"}),
                                ("agent-maya", {"candidate_id": "wrong"}),
                                ("agent-maya", {"plan_id": "wrong"})):
            await self.review(p, "approved", sender, **changes)
        self.assertEqual(self.events(p), [])
        await self.review(p, "approval_required")
        await self.review(p, "approval_required")
        await self.review(p, "unknown")
        self.assertEqual(self.kinds(p), [E.APPROVAL_REQUIRED])
        self.assertEqual(self.ctx.session, p.session)
        self.app.negotiations[p.id]["status"] = "finished"
        await self.review(p, "approved")
        await self.review(p, "rejected")
        self.assertEqual(self.kinds(p), [E.APPROVAL_REQUIRED])

    async def test_late_approval_expires_once_without_rescoring(self):
        p = self.plan(round=2)
        self.negotiation(p)
        self.app.negotiations[p.id]["started"] -= self.app.NEGOTIATION_TIMEOUT + 1
        await self.review(p, "approved", "stranger")
        self.assertEqual(self.events(p), [])
        await self.review(p, "approved")
        await self.app.check_plan_timeouts(self.ctx)
        await self.review(p, "approved")
        self.assertEqual(self.kinds(p), [E.NEGOTIATION_TIMEOUT])
        self.assertNotIn(p.id, self.app.plans)
        self.assertNotIn(p.id, self.app.negotiations)
        self.assertEqual(self.ctx.session, p.session)

    async def test_peer_refusal_and_inability_are_not_human_rejection(self):
        for status in ("rejected", "not_possible"):
            p = self.plan(round=2)
            self.negotiation(p)
            await self.review(p, status)
            await self.review(p, status)
            self.assertEqual(self.kinds(p), [E.REJECTED])
            self.assertNotIn("owner rejected", self.app.activity_traces[p.id].render())
            self.assertNotIn(p.id, self.app.plans)

    async def test_timeout_sweep_distinguishes_scoring_and_review(self):
        p = self.plan()
        p.t0 -= self.app.TIMEOUT + 1
        await self.app.check_plan_timeouts(self.ctx)
        self.assertEqual(self.kinds(p), [E.AGENT_TIMEOUT])
        self.assertIn("INCOMPLETE", self.ctx.texts()[-1])
        q = self.plan(round=2)
        q.t0 -= self.app.TIMEOUT + 1
        self.negotiation(q)
        await self.app.check_plan_timeouts(self.ctx)
        self.assertEqual(self.events(q), [])
        self.app.negotiations[q.id]["started"] -= self.app.NEGOTIATION_TIMEOUT + 1
        await self.app.check_plan_timeouts(self.ctx)
        self.assertEqual(self.kinds(q), [E.NEGOTIATION_TIMEOUT])

    async def test_complete_expired_round_is_not_an_agent_timeout(self):
        p = self.plan()
        self.fill(p)
        p.t0 -= self.app.TIMEOUT + 1
        await self.app.check_plan_timeouts(self.ctx)
        self.assertEqual(self.kinds(p), [E.PLAN_PROPOSED])

    async def test_invalid_partial_scores_are_incomplete_not_infeasible(self):
        for scores in ([], [self.app.CandidateScore(candidate_id=self.candidate.id,
                                                  veto=False, score=2, reason="invalid")]):
            p = self.plan()
            self.fill(p)
            p.replies["maya"].scores = scores
            await self.app.finalize_peer_plan(self.ctx, p)
            self.assertEqual(self.events(p), [])
            self.assertIn("INCOMPLETE", self.ctx.texts()[-1])
            self.assertNotIn(p.id, self.app.plans)

    async def test_failed_rescore_does_not_repeat_negotiation(self):
        p = self.plan(round=2)
        p.negotiation_attempted = True
        self.fill(p, blocker="maya")
        await self.app.finalize_peer_plan(self.ctx, p)
        self.assertEqual(self.kinds(p), [E.NO_FEASIBLE_PLAN])
        self.assertNotIn(p.id, self.app.plans)
        self.assertEqual(self.app.negotiations, {})

    async def test_live_places_pool_metadata_and_cached_rounds(self):
        os.environ["GOOGLE_PLACES_API_KEY"] = "offline-placeholder"
        def search(area, slot):
            self.assertEqual(self.app.plans, {})
            return [SimpleNamespace(**{**vars(self.candidate), "id": f"live@{slot}", "slot": slot,
                                       "maps_url": "https://example.invalid/map", "reservable": False})], []
        self.places.search.side_effect = search
        await self.chat("plan")
        p = next(iter(self.app.plans.values()))
        self.assertEqual(self.places.search.call_count, 3)
        self.assertEqual(len(p.live), 3)
        self.assertEqual(len(p.candidates), 2)
        self.assertIs(self.app.reservable[self.candidate.id], False)
        self.assertEqual(self.app.maps_links[self.candidate.id], "https://example.invalid/map")
        p.round = 2
        await self.app.begin_round(self.ctx, p)
        p.negotiation_attempted = True
        await self.app.begin_round(self.ctx, p)
        self.assertEqual(len(p.candidates), 3)
        self.assertEqual(self.places.search.call_count, 3)

    async def test_places_failure_preserves_fixture_fallback(self):
        os.environ["GOOGLE_PLACES_API_KEY"] = "offline-placeholder"
        self.places.search.side_effect = RuntimeError("offline failure")
        await self.chat("plan")
        p = next(iter(self.app.plans.values()))
        self.assertIsNone(p.live)
        self.assertEqual(len(p.candidates), 2)
        self.assertIn("demo restaurant list", self.ctx.texts()[0])
        self.assertEqual(self.kinds(p)[0], E.PLAN_STARTED)

    async def test_proposal_persistence_cleanup_trace_status_and_book(self):
        p = self.plan()
        self.fill(p)
        self.app.maps_links[self.candidate.id] = "https://example.invalid/map"
        calls = []
        def tiger(fn, *args):
            self.assertIs(fn, self.memory.record_plan)
            self.assertIn(p.id, self.app.plans)
            self.assertEqual(self.app.last_plans[p.user]["id"], self.candidate.id)
            self.assertNotIn(E.PLAN_PROPOSED, self.kinds(p))
            calls.append(args)
            return None  # same result as optional/offline persistence
        original_trace = self.app.trace_event
        def trace(*args, **kwargs):
            if args[1] == E.PLAN_PROPOSED:
                self.assertEqual(len(calls), 1)
                self.assertIn(p.id, self.app.plans)
            original_trace(*args, **kwargs)
        with patch.object(self.app, "tiger", side_effect=tiger), patch.object(self.app, "trace_event", side_effect=trace):
            await self.app.finalize_peer_plan(self.ctx, p)
        self.assertNotIn(p.id, self.app.plans)
        self.assertEqual(self.kinds(p), [E.PLAN_PROPOSED])
        self.assertEqual(self.ctx.session, p.session)
        self.assertIn("Live Venue", self.ctx.texts()[-1])
        self.assertIn("Calendar draft", self.ctx.texts()[-1])
        self.assertIn("https://example.invalid/map", self.ctx.texts()[-1])
        fresh = FakeContext("new-owner-chat")
        await self.chat("TRACE", ctx=fresh)
        self.assertIn("feasible group proposal", fresh.texts()[-1])
        self.assertEqual(fresh.session, "new-owner-chat")
        await self.chat("STATUS", ctx=fresh)
        self.assertEqual(fresh.texts()[-1], self.app.latest_status[p.user])
        self.booking.check_availability.side_effect = None
        self.booking.check_availability.return_value = SimpleNamespace(status="ready")
        await self.chat("BOOK", ctx=fresh)
        self.booking.check_availability.assert_called_once_with(
            self.candidate.name, SLOT, 4, self.app.PLAN_AREA.split(",")[-1].strip())
        self.assertEqual(fresh.texts()[-1], "Availability only; not booked")
        self.app.reservable[self.candidate.id] = False
        await self.chat("BOOK", ctx=fresh)
        self.assertEqual(self.booking.check_availability.call_count, 1)
        self.assertIn("walk in", fresh.texts()[-1])
        self.nl.propose.assert_not_called()

    async def test_notification_enrollment_authorization_and_exact_command(self):
        await self.chat("NOTIFY HERE", sender="stranger")
        self.assertEqual(self.app.owner_notification_sessions, {})
        os.environ["ALEX_OWNER_ADDRESSES"] += ",agent-maya"
        await self.chat("NOTIFY HERE", sender="agent-maya")
        self.assertEqual(self.app.owner_notification_sessions, {})
        await self.chat("NOTIFY HERE")
        await self.chat("notify here", sender="owner-b", ctx=FakeContext("second-owner-chat"))
        self.assertEqual(self.app.owner_notification_sessions,
                         {"owner-a": "current-chat", "owner-b": "second-owner-chat"})
        await self.chat("NOTIFY HERE", ctx=FakeContext("replacement-chat"))
        self.assertEqual(self.app.owner_notification_sessions["owner-a"], "replacement-chat")
        self.nl.propose.assert_not_called()
        self.save_persona.assert_not_called()

    async def incoming_offer(self):
        os.environ["ALEX_OWNER_ADDRESSES"] = "owner-a"
        costly = self.app.Candidate(**{**vars(self.candidate), "price": 120})
        request = self.app.CounterofferRequest(plan_id="peer-plan", candidate=costly, category="budget")
        await self.app.on_counteroffer_request(self.ctx, "agent-maya", request)
        return self.app.pending_counteroffers["peer-plan"]

    async def test_notification_routes_chat_schema_protocol_and_restores_peer_session(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        offer = await self.incoming_offer()
        kwargs, session = self.ctx.raw_sent[0]
        self.assertEqual(session, "private-owner-chat")
        self.assertEqual(kwargs["destination"], "owner-a")
        self.assertEqual(kwargs["protocol_digest"], "chat-protocol-digest")
        self.assertEqual(kwargs["message_schema_digest"], "chat-message-schema")
        message = json.loads(kwargs["message_body"])
        self.assertEqual(message["msg_id"], str(offer["notification"]["message_id"]))
        self.assertIn("PRIVATE COUNTEROFFER", message["content"][0]["text"])
        self.assertIn("APPROVE peer-plan", message["content"][0]["text"])
        self.assertEqual(self.ctx.session, "current-chat")
        target, response, response_session = self.ctx.sent[-1]
        self.assertEqual((target, response.status, response_session),
                         ("agent-maya", "approval_required", "current-chat"))
        self.assertEqual(offer["notification"]["transport_status"], "delivered")
        self.assertFalse(offer["notification"]["acknowledged"])
        self.assertNotIn("owner-a", str(self.ctx.logger.mock_calls))
        self.assertNotIn("PRIVATE COUNTEROFFER", str(self.ctx.logger.mock_calls))
        self.assertNotIn("budget", str(self.ctx.logger.mock_calls))
        self.save_persona.assert_not_called()

    async def test_notification_transport_failures_preserve_offer_and_restore_session(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        for status in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, None):
            with self.subTest(status=status):
                self.app.pending_counteroffers.clear()
                self.ctx.send_raw.side_effect = None
                self.ctx.send_raw.return_value = SimpleNamespace(status=status)
                offer = await self.incoming_offer()
                self.assertEqual(offer["notification"]["transport_status"],
                                 status.value if status else "unknown")
                self.assertFalse(offer["notification"]["acknowledged"])
                self.assertEqual(self.ctx.session, "current-chat")
                self.assertEqual(self.ctx.sent[-1][1].status, "approval_required")
        self.app.pending_counteroffers.clear()
        self.ctx.send_raw.side_effect = RuntimeError("private-secret-sentinel")
        offer = await self.incoming_offer()
        self.assertEqual(offer["notification"]["transport_status"], "exception")
        self.assertEqual(self.ctx.session, "current-chat")
        self.assertEqual(self.ctx.sent[-1][1].status, "approval_required")
        self.assertNotIn("private-secret-sentinel", str(self.ctx.logger.mock_calls))
        await self.chat("OFFER")
        self.assertIn("APPROVE peer-plan", self.ctx.texts()[-1])

    async def test_notification_ack_requires_owner_session_and_message_id(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        offer = await self.incoming_offer()
        record = offer["notification"]
        ack = FakeModel(acknowledged_msg_id=record["message_id"])
        private = FakeContext("private-owner-chat")
        for sender, ctx, message in (
            ("stranger", private, ack), ("agent-maya", private, ack),
            ("owner-a", self.ctx, ack),
            ("owner-a", private, FakeModel(acknowledged_msg_id="unknown"))):
            await self.app.on_ack(ctx, sender, message)
            self.assertFalse(record["acknowledged"])
        os.environ["ALEX_OWNER_ADDRESSES"] = "owner-b"
        await self.app.on_ack(private, "owner-a", ack)
        self.assertFalse(record["acknowledged"])
        os.environ["ALEX_OWNER_ADDRESSES"] = "owner-a"
        # Late ack after offer cleanup still records transport evidence, not approval.
        self.app.pending_counteroffers.clear()
        await self.app.on_ack(private, "owner-a", ack)
        await self.app.on_ack(private, "owner-a", ack)
        self.assertTrue(record["acknowledged"])
        private.logger.info.assert_called_once()
        self.save_persona.assert_not_called()
        self.assertEqual(self.app.activity_traces, {})

    async def test_notification_ack_can_arrive_during_send(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        async def send(**kwargs):
            message_id = next(iter(self.app.owner_notifications))
            await self.app.on_ack(FakeContext("private-owner-chat"), "owner-a",
                                  FakeModel(acknowledged_msg_id=message_id))
            return SimpleNamespace(status=DeliveryStatus.SENT)
        self.ctx.send_raw.side_effect = send
        offer = await self.incoming_offer()
        self.assertTrue(offer["notification"]["acknowledged"])
        self.assertEqual(offer["notification"]["transport_status"], "sent")

    async def test_notification_cancellation_restores_session(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        self.ctx.send_raw.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.incoming_offer()
        self.assertEqual(self.ctx.session, "current-chat")
        self.assertIn("peer-plan", self.app.pending_counteroffers)

    async def test_notified_offer_rejection_does_not_save_preferences(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        await self.incoming_offer()
        await self.chat("REJECT peer-plan")
        self.assertNotIn("peer-plan", self.app.pending_counteroffers)
        self.save_persona.assert_not_called()
        self.assertTrue(any(getattr(message, "status", None) == "rejected"
                            for _, message, _ in self.ctx.sent))

    async def test_notified_offer_approval_preserves_workflow(self):
        self.app.owner_notification_sessions["owner-a"] = "private-owner-chat"
        await self.incoming_offer()
        with patch.object(self.app, "remember_persona") as persist:
            await self.chat("APPROVE peer-plan", ctx=FakeContext("private-owner-chat"))
        self.save_persona.assert_called_once()
        persist.assert_called_once()
        self.assertNotIn("peer-plan", self.app.pending_counteroffers)
        self.ctx.send_raw.assert_awaited_once()

    async def test_private_offer_and_approval_keep_save_order_and_private_data_out(self):
        os.environ["ALEX_OWNER_ADDRESSES"] = "owner-a"
        costly = self.app.Candidate(**{**vars(self.candidate), "price": 120})
        request = self.app.CounterofferRequest(plan_id="peer-plan", candidate=costly, category="budget")
        await self.app.on_counteroffer_request(self.ctx, "agent-maya", request)
        self.assertIn("peer-plan", self.app.pending_counteroffers)
        self.ctx.send_raw.assert_not_called()  # no enrollment; OFFER still recovers it
        self.assertEqual(self.ctx.sent[-1][1].status, "approval_required")
        await self.chat("OFFER")
        self.assertIn("APPROVE peer-plan", self.ctx.texts()[-1])
        self.assertEqual(self.app.activity_traces, {})
        def remember(updated):
            self.save_persona.assert_called_once()
            self.assertIs(self.app.persona, updated)
            self.assertIn("peer-plan", self.app.pending_counteroffers)
        with patch.object(self.app, "remember_persona", side_effect=remember) as persist:
            await self.chat("APPROVE peer-plan")
        persist.assert_called_once()
        self.assertNotIn("peer-plan", self.app.pending_counteroffers)
        statuses = [message.status for _, message, _ in self.ctx.sent
                    if isinstance(message, self.app.CounterofferReply)]
        self.assertEqual(statuses, ["approval_required", "approved"])
        self.assertEqual(self.app.activity_traces, {})
        self.nl.propose.assert_not_called()

    async def proposed_plan(self):
        p = self.plan()
        self.fill(p)
        p.t0 -= self.app.TIMEOUT + 1
        await self.app.check_plan_timeouts(self.ctx)
        return p

    async def test_proposed_plan_carries_review_card(self):
        p = await self.proposed_plan()
        target, message, _ = self.ctx.sent[-1]
        self.assertEqual(target, "owner-a")
        text, meta = message.content
        self.assertIn("PROPOSED GROUP PLAN", text.text)          # plain text stays as the fallback
        self.assertEqual((meta.type, meta.metadata["card_kind"], meta.metadata["card_protocol_version"]),
                         ("metadata", "review", "1"))
        card = json.loads(meta.metadata["card_payload"])
        self.assertEqual(card["approve_cta"]["selection"], {"action": "approve_plan", "plan_id": p.id})
        self.assertEqual(card["reject_cta"]["selection"], {"action": "reject_plan", "plan_id": p.id})
        self.assertIn("Live Venue", card["title"])

    async def test_review_card_approve_checks_a_table_both_reply_shapes(self):
        p = await self.proposed_plan()
        self.booking.check_availability.side_effect = None
        await self.chat(json.dumps({"action": "approve_plan", "plan_id": p.id}))          # @mentioned: JSON
        await self.chat(f"The user approved the plan {p.id} and wants a table.")         # planner: prose
        self.assertEqual(self.booking.check_availability.call_count, 2)
        self.assertIn("Availability only; not booked", self.ctx.texts()[-1])
        self.nl.propose.assert_not_called()                                               # never read as a preference

    async def test_review_card_reject_and_stale_plans_never_book(self):
        p = await self.proposed_plan()
        await self.chat(json.dumps({"action": "approve_plan", "plan_id": "deadbeef"}))
        self.assertIn("no longer active", self.ctx.texts()[-1])
        await self.chat(json.dumps({"action": "reject_plan", "plan_id": p.id}))
        self.assertIn("declined. Nothing was booked", self.ctx.texts()[-1])
        await self.chat(json.dumps({"action": "approve_plan", "plan_id": p.id}))          # already declined
        self.assertIn("no longer active", self.ctx.texts()[-1])
        self.booking.check_availability.assert_not_called()


if __name__ == "__main__":
    unittest.main()
