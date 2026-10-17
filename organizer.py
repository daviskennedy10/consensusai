"""Organizer agent: python organizer.py. Accessible through ASI:One with mailbox."""
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents.crypto import Identity
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec
from models import Candidate, ScoreReply, ScoreRequest
from scoring import choose, build_complete_table

BASE=Path(__file__).resolve().parent
personas=json.loads((BASE/'personas.json').read_text())
venues=json.loads((BASE/'venues.json').read_text())
ADDR={n:Identity.from_seed(p['seed'],0).address for n,p in personas.items()}
REVERSE={v:k for k,v in ADDR.items()}
TIMEOUT=12
SLOTS={1:['2026-10-23T19:00','2026-10-24T19:00'],2:['2026-10-23T19:00','2026-10-24T19:00','2026-10-25T18:00']}
agent=Agent(name='consensusai-organizer',seed=os.environ.get('ORGANIZER_SEED','change-me-organizer-seed'),port=8000,mailbox=True)
chat=Protocol(spec=chat_protocol_spec)

class Plan:
    def __init__(self,user):
        self.id=uuid4().hex[:8]
        self.user=user
        self.round=1
        self.replies={}
        self.candidates=[]
        self.t0=time.monotonic()
        self.finalizing=False

plans={}

def candidates(round_no):
    return [Candidate(id=f"{v['id']}@{slot}",name=v['name'],cuisine=v['cuisine'],price=v['price'],distance_min=v['distance_min'],rating=v['rating'],tags=v['tags'],slot=slot) for v in venues for slot in SLOTS[round_no]]

async def say(ctx,to,text):
    await ctx.send(to,ChatMessage(timestamp=datetime.now(timezone.utc),msg_id=uuid4(),content=[TextContent(type='text',text=text)]))

async def broadcast(ctx,plan):
    plan.replies={}
    plan.candidates=candidates(plan.round)
    plan.t0=time.monotonic()
    plan.finalizing=False
    req=ScoreRequest(plan_id=plan.id,round=plan.round,candidates=plan.candidates)
    for name,addr in ADDR.items():
        try:
            await ctx.send(addr,req)
        except Exception as exc:
            ctx.logger.error('Send failed to %s: %s',name,exc)

def calendar_link(title,slot,where):
    start=datetime.fromisoformat(slot)
    fmt='%Y%m%dT%H%M%S'
    q=urllib.parse.urlencode({'action':'TEMPLATE','text':title,'location':where,
                              'dates':f'{start.strftime(fmt)}/{(start+timedelta(hours=2)).strftime(fmt)}'})
    return 'https://calendar.google.com/calendar/render?'+q

async def finalize(ctx,plan):
    if plan.finalizing:return
    plan.finalizing=True
    missing=[name for name in ADDR if name not in plan.replies]
    if missing:
        plans.pop(plan.id,None)
        await say(ctx,plan.user,'INCOMPLETE — no group decision made. Missing agents: '+', '.join(missing)+'. Their deal breakers are unknown; no restaurant selected and no calendar link generated. Restart the missing agents and send plan again.')
        return
    table=build_complete_table(plan.replies,plan.candidates,ADDR)
    if table is None:
        plans.pop(plan.id,None)
        await say(ctx,plan.user,'INCOMPLETE — an agent returned invalid or partial scores. No group decision made; please retry.')
        return
    if not table:
        if plan.round==1:
            plan.round=2
            await say(ctx,plan.user,'All options vetoed; searching an additional date. Hard constraints unchanged.')
            await broadcast(ctx,plan)
            return
        plans.pop(plan.id,None)
        reasons=sorted({f'{name}: {s.reason}' for name,rep in plan.replies.items() for s in rep.scores if s.veto})[:6]
        await say(ctx,plan.user,'NO FEASIBLE GROUP PLAN. Hard constraints were NOT relaxed. Ask individuals to update their own preferences and retry. Reasons:\n'+'\n'.join(reasons))
        return
    plans.pop(plan.id,None)
    fair,avg=choose(table)
    venue_id,slot=fair.split('@',1)
    venue=next(x for x in venues if x['id']==venue_id)
    worst=min(table[fair],key=table[fair].get)
    lines=[f"PROPOSED plan: {venue['name']} ({venue['cuisine']}) at {slot}, approx ${venue['price']} per person.",
           f"Min satisfaction {worst}: {table[fair][worst]:.2f}; average {sum(table[fair].values())/len(table[fair]):.2f}."]
    if avg!=fair:
        weakest=min(table[avg],key=table[avg].get)
        lines.append(f"Highest-average alternative would leave {weakest} at {table[avg][weakest]:.2f}; fairness objective favored this plan.")
    lines.append('Calendar link (opens a draft; does NOT create invites automatically): '+calendar_link('Dinner at '+venue['name'],slot,venue['name']))
    lines.append('Agent scores are not attendee consent. Confirm with all humans before booking or payment.')
    await say(ctx,plan.user,'\n'.join(lines))

@chat.on_message(ChatMessage)
async def on_chat(ctx:Context,sender:str,msg:ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id
        )
    )

    # Get the message received from ASI:One
    text = msg.text().strip().lower()

    # Remove any @agent address prefixes added by ASI:One
    import re

    # Remove an agent mention only at the beginning.
    text = re.sub(r'^@agent1[a-z0-9]+\s*', '', text).strip()

    # Log the cleaned message for debugging
    ctx.logger.info("Received chat command: %r", text)

    # Ignore empty messages to avoid unnecessary replies
    if not text:
        return

    if text not in ('plan', 'plan dinner', 'start', 'help'):
        ctx.logger.info(
        "Ignoring unrecognized message from %s: %.100r",
        sender,
        text
        )
        return
    if text=='help':
        await say(ctx,sender,'Type PLAN to have three agents privately score our fixed dinner dataset; configure each persona in their own chat first. No real booking or payment occurs.')
        return
    if any(p.user==sender for p in plans.values()):
        await say(ctx,sender,'Your existing request is still running; please do not duplicate it.')
        return
    plan=Plan(sender)
    plans[plan.id]=plan
    await say(ctx,sender,f'Checking {len(ADDR)} personal agents for plan {plan.id}. Every agent must reply before any result is proposed.')
    await broadcast(ctx,plan)

@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx:Context,sender:str,msg:ChatAcknowledgement):
    pass

@agent.on_message(ScoreReply)
async def on_reply(ctx:Context,sender:str,msg:ScoreReply):
    plan=plans.get(msg.plan_id)
    if not plan or plan.finalizing or msg.round!=plan.round:return
    if REVERSE.get(sender)!=msg.persona or msg.persona in plan.replies:
        ctx.logger.warning('Rejected unexpected or duplicate scoring reply from %s',sender)
        return
    plan.replies[msg.persona]=msg
    if len(plan.replies)==len(ADDR):
        await finalize(ctx,plan)

@agent.on_interval(period=1.0)
async def sweep(ctx:Context):
    for plan in list(plans.values()):
        if not plan.finalizing and time.monotonic()-plan.t0>=TIMEOUT:
            await finalize(ctx,plan)

agent.include(chat,publish_manifest=True)
if __name__=='__main__':agent.run()
