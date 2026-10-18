"""Run: python personal_agent.py maya. Configure through its ASI:One chat."""
import re
import time
import urllib.parse
from datetime import timedelta

from coordination import Plan
from scoring import choose, build_complete_table
from uagents.crypto import Identity

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec

from models import Candidate, CandidateScore, ScoreReply, ScoreRequest
from preferences import load_persona, parse_update, save_persona, validate_update
from scoring import evaluate

BASE = Path(__file__).resolve().parent
# Shared demo data
personas = json.loads((BASE / "personas.json").read_text())
venues = json.loads((BASE / "venues.json").read_text())

# Every personal agent knows the other agents' public addresses.
ADDR = {
    n: Identity.from_seed(p["seed"], 0).address
    for n, p in personas.items()
}
REVERSE = {addr: n for n, addr in ADDR.items()}

TIMEOUT = 12

SLOTS = {
    1: ["2026-10-23T19:00", "2026-10-24T19:00"],
    2: [
        "2026-10-23T19:00",
        "2026-10-24T19:00",
        "2026-10-25T18:00",
    ],
}

# Planning sessions coordinated by THIS personal agent.
plans = {}
if len(sys.argv) != 2 or sys.argv[1] not in json.loads((BASE/'personas.json').read_text()):
    raise SystemExit('Usage: python personal_agent.py maya|jordan|sam|alex')
name = sys.argv[1]
persona = load_persona(name, BASE)
port = persona['port']
agent = Agent(name=f'consensusai-{name}', seed=persona['seed'], port=port, mailbox=True)
chat = Protocol(spec=chat_protocol_spec)

async def reply(ctx, target, message):
    await ctx.send(target, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type='text',text=message)]))

def get_candidates(round_no):
    return [
        Candidate(
            id=f"{v['id']}@{slot}",
            name=v["name"],
            cuisine=v["cuisine"],
            price=v["price"],
            distance_min=v["distance_min"],
            rating=v["rating"],
            tags=v["tags"],
            slot=slot,
        )
        for v in venues
        for slot in SLOTS[round_no]
    ]


def calendar_link(title, slot, location):
    start = datetime.fromisoformat(slot)
    end = start + timedelta(hours=2)

    fmt = "%Y%m%dT%H%M%S"

    params = urllib.parse.urlencode({
        "action": "TEMPLATE",
        "text": title,
        "location": location,
        "dates": f"{start.strftime(fmt)}/{end.strftime(fmt)}",
    })

    return "https://calendar.google.com/calendar/render?" + params


async def begin_round(ctx, plan):
    plan.begin_round(get_candidates(plan.round))

    request = plan.make_request()

    # IMPORTANT:
    # The coordinating agent participates in the decision.
    # Score candidates using its own private preferences.
    own_scores = []

    for candidate in plan.candidates:
        veto, score, reason = evaluate(persona, candidate)

        own_scores.append(
            CandidateScore(
                candidate_id=candidate.id,
                veto=veto,
                score=score,
                reason=reason,
            )
        )

    plan.replies[name] = ScoreReply(
        plan_id=plan.id,
        round=plan.round,
        persona=name,
        scores=own_scores,
    )

    ctx.logger.info(
        "%s scored %d candidates locally",
        name,
        len(own_scores),
    )

    # Ask every OTHER agent to evaluate the same candidates.
    for peer_name, address in plan.participants.items():
        if peer_name == name:
            continue

        try:
            await ctx.send(address, request)

            ctx.logger.info(
                "Sent plan %s to %s",
                plan.id,
                peer_name,
            )

        except Exception as exc:
            ctx.logger.error(
                "Could not contact %s: %s",
                peer_name,
                exc,
            )


async def finalize_peer_plan(ctx, plan):
    if plan.finalizing:
        return

    plan.finalizing = True

    missing = plan.missing_agents()

    if missing:
        plans.pop(plan.id, None)

        await reply(
            ctx,
            plan.user,
            "INCOMPLETE: Missing responses from "
            + ", ".join(missing)
            + ". No group decision was made.",
        )
        return

    table = build_complete_table(
        plan.replies,
        plan.candidates,
        plan.participants,
    )

    if table is None:
        plans.pop(plan.id, None)
        await reply(
            ctx,
            plan.user,
            "INCOMPLETE: Invalid or partial agent scores.",
        )
        return

    if not table:
        if plan.round == 1:
            plan.round = 2

            await reply(
                ctx,
                plan.user,
                "All candidates were vetoed. "
                "Trying an additional date without relaxing deal breakers.",
            )

            await begin_round(ctx, plan)
            return

        plans.pop(plan.id, None)

        await reply(
            ctx,
            plan.user,
            "NO FEASIBLE PLAN: All candidates violate "
            "at least one participant's hard constraints. "
            "No constraints were automatically relaxed.",
        )
        return

    fair, average_choice = choose(table)

    venue_id, slot = fair.split("@", 1)
    venue = next(v for v in venues if v["id"] == venue_id)

    scores = table[fair]
    lowest_person = min(scores, key=scores.get)
    average_score = sum(scores.values()) / len(scores)

    lines = [
        f"PROPOSED GROUP PLAN (coordinated by {name})",
        f"Restaurant: {venue['name']}",
        f"Cuisine: {venue['cuisine']}",
        f"Time: {slot}",
        f"Price: approximately ${venue['price']} per person",
        "",
        f"Minimum satisfaction: {lowest_person} = "
        f"{scores[lowest_person]:.2f}",
        f"Average satisfaction: {average_score:.2f}",
    ]

    if average_choice != fair:
        alternative = table[average_choice]
        weakest = min(alternative, key=alternative.get)

        lines.append(
            f"Highest-average alternative would leave "
            f"{weakest} at {alternative[weakest]:.2f}."
        )

    lines.append("")
    lines.append(
        "Calendar draft: "
        + calendar_link(
            f"Dinner at {venue['name']}",
            slot,
            venue["name"],
        )
    )

    lines.append(
        "This is a proposal, not a booking. "
        "All participants must approve before any reservation or payment."
    )

    plans.pop(plan.id, None)

    await reply(ctx, plan.user, "\n".join(lines))
@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    global persona
    ctx.logger.info("Incoming chat from sender: %s", sender)
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = msg.text().strip()
    text = re.sub(
    r"^@agent1[a-z0-9]+\s*",
    "",
    text,
    flags=re.IGNORECASE,
    ).strip()
    # This is NOT real authentication. For a public demo, configure a private allowlist.
    allowed = [x.strip() for x in os.getenv(f'{name.upper()}_OWNER_ADDRESSES','').split(',') if x.strip()]
    if not allowed or sender not in allowed:
        await reply(ctx, sender, 'Preference edits disabled. Set '+name.upper()+'_OWNER_ADDRESSES to the allowed ASI:One sender address. Ask the agent operator to configure it.')
        return
        # A personal agent can initiate a group planning session.
    if text.lower() in ("plan", "plan dinner"):
        if any(p.user == sender for p in plans.values()):
            await reply(
                ctx,
                sender,
                "You already have an active planning session.",
            )
            return

        plan = Plan(
            user=sender,
            participants=ADDR.copy(),
            coordinator=agent.address,
        )

        plans[plan.id] = plan

        await reply(
            ctx,
            sender,
            f"{name.title()} is coordinating plan {plan.id}. "
            f"All {len(ADDR)} personal agents will participate.",
        )

        await begin_round(ctx, plan)
        return
    if text.lower() in ('help', 'start'):
        await reply(ctx, sender, 'Commands: show | set max_price 25 | set max_distance_min 20 | set dietary vegetarian,halal | set free_slots 2026-10-23T19:00,2026-10-24T19:00 | set weights {"cuisine":4,"rating":3,"distance":2,"price":1} | set cuisine_pref {"thai":1,"italian":0.5}. Send confirm before the update is applied.')
    elif text.lower() == 'show':
        await reply(ctx, sender, json.dumps({'hard':persona['hard'],'weights':persona['weights'],'cuisine_pref':persona['cuisine_pref']}, indent=2))
    elif text.lower() == 'confirm':
        pending = ctx.storage.get(f'pending_{sender}')
        if not pending:
            await reply(ctx, sender, 'Nothing pending. Send a set command first.')
        else:
            try:
                updated=validate_update(persona,json.loads(pending))
                save_persona(name, BASE, updated)
                persona=updated
                ctx.storage.set(f'pending_{sender}','')
                await reply(ctx, sender, 'Saved. Updated preference: '+pending)
            except (ValueError,TypeError) as e:
                await reply(ctx,sender,'Cannot apply update: '+str(e))
    elif text.lower() == 'cancel':
        ctx.storage.set(f'pending_{sender}','')
        await reply(ctx,sender,'Pending preference update cancelled.')
    else:
        try:
            update=parse_update(text)
            validate_update(persona,update)
            ctx.storage.set(f'pending_{sender}',json.dumps(update))
            await reply(ctx,sender,'Proposed change: '+json.dumps(update)+'\nReply confirm to save, or cancel.')
        except (ValueError,TypeError,json.JSONDecodeError) as e:
            await reply(ctx,sender,str(e)+'\nSend help for examples.')

@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass

@agent.on_message(ScoreRequest)
async def on_score(ctx: Context, sender: str, msg: ScoreRequest):
    allowed_coordinators = set(ADDR.values())

    # Keep the original organizer as a fallback.
    fallback = os.getenv("ORGANIZER_ADDRESS", "")
    if fallback:
        allowed_coordinators.add(fallback)

    if sender not in allowed_coordinators:
        ctx.logger.warning('Rejected scoring request from unrecognized organizer %s',sender)
        return
    scores=[]
    for c in msg.candidates:
        veto,score,reason=evaluate(persona,c)
        scores.append(CandidateScore(candidate_id=c.id,veto=veto,score=score,reason=reason))
    await ctx.send(sender,ScoreReply(plan_id=msg.plan_id,round=msg.round,persona=name,scores=scores))

@agent.on_message(ScoreReply)
async def on_peer_score(ctx: Context, sender: str, msg: ScoreReply):
    plan = plans.get(msg.plan_id)

    if not plan:
        return

    if plan.finalizing or msg.round != plan.round:
        return

    # Verify the reply came from the agent it claims to represent.
    if REVERSE.get(sender) != msg.persona:
        ctx.logger.warning(
            "Rejected score reply from unexpected sender %s",
            sender,
        )
        return

    if msg.persona in plan.replies:
        return

    plan.replies[msg.persona] = msg

    ctx.logger.info(
        "Plan %s: received %d/%d responses",
        plan.id,
        len(plan.replies),
        len(plan.participants),
    )

    if plan.is_complete():
        await finalize_peer_plan(ctx, plan)


@agent.on_interval(period=1.0)
async def check_plan_timeouts(ctx: Context):
    for plan in list(plans.values()):
        if not plan.finalizing and plan.expired(TIMEOUT):
            await finalize_peer_plan(ctx, plan)

agent.include(chat,publish_manifest=True)
if __name__=='__main__':
    agent.run()
