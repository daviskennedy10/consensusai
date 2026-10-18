"""Run: python personal_agent.py maya. Configure through its ASI:One chat."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec

from models import CandidateScore, ScoreReply, ScoreRequest
from preferences import load_persona, parse_update, save_persona, validate_update
from scoring import evaluate

BASE = Path(__file__).resolve().parent
if len(sys.argv) != 2 or sys.argv[1] not in json.loads((BASE/'personas.json').read_text()):
    raise SystemExit('Usage: python personal_agent.py maya|jordan|sam|alex')
name = sys.argv[1]
persona = load_persona(name, BASE)
port = persona['port']
agent = Agent(name=f'consensusai-{name}', seed=persona['seed'], port=port, mailbox=True)
chat = Protocol(spec=chat_protocol_spec)

async def reply(ctx, target, message):
    await ctx.send(target, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type='text',text=message)]))

@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    global persona
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = msg.text().strip()
    # This is NOT real authentication. For a public demo, configure a private allowlist.
    allowed = [x.strip() for x in os.getenv(f'{name.upper()}_OWNER_ADDRESSES','').split(',') if x.strip()]
    if not allowed or sender not in allowed:
        await reply(ctx, sender, 'Preference edits disabled. Set '+name.upper()+'_OWNER_ADDRESSES to the allowed ASI:One sender address. Ask the agent operator to configure it.')
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
    allowed=os.getenv('ORGANIZER_ADDRESS','')
    if not allowed or sender != allowed:
        ctx.logger.warning('Rejected scoring request from unrecognized organizer %s',sender)
        return
    scores=[]
    for c in msg.candidates:
        veto,score,reason=evaluate(persona,c)
        scores.append(CandidateScore(candidate_id=c.id,veto=veto,score=score,reason=reason))
    await ctx.send(sender,ScoreReply(plan_id=msg.plan_id,round=msg.round,persona=name,scores=scores))

agent.include(chat,publish_manifest=True)
if __name__=='__main__':
    agent.run()
