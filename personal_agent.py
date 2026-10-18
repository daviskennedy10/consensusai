"""Run: python personal_agent.py maya. Configure through its ASI:One chat.
Live venues, plain-text preferences, table checks and group memory need GOOGLE_PLACES_API_KEY,
STEP_API_KEY, BROWSER_USE_API_KEY and DATABASE_URL in the environment (set -a; . ./.env; set +a).
Each one is optional: without it the agent uses venues.json, `set`, a booking link, and local files."""
import asyncio
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

from models import (
    Candidate,
    CandidateScore,
    ScoreReply,
    ScoreRequest,
    CounterofferRequest,
    CounterofferReply,
)
import booking
import fairness
import google_calendar
import nl_preferences
import venues as places
from preferences import load_persona, save_persona, validate_update

from negotiation import (
    counteroffers,
    propose_compromise,
    minimal_relaxation,
    describe_relaxation,
    near_misses,
)

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
latest_status = {}
# Counteroffers awaiting the owner of THIS personal agent.
pending_counteroffers = {}
# Negotiations coordinated by THIS agent.
negotiations = {}

# Give humans three minutes to respond during the demo.
NEGOTIATION_TIMEOUT = 180

# Live venues come from Google Places near this area.
PLAN_AREA = os.getenv("PLAN_AREA", "Westwood, Los Angeles")
# Google Maps links for live candidates, by candidate id.
maps_links = {}
# Google's "takes reservations" flag for live candidates (None = unknown), by candidate id.
reservable = {}
# Each user's latest proposed plan, for BOOK.
last_plans = {}
# Plans are recorded in Tiger under the demo group that memory.seed_demo creates.
GROUP_ID = "demo"
if len(sys.argv) != 2 or sys.argv[1] not in json.loads((BASE/'personas.json').read_text()):
    raise SystemExit('Usage: python personal_agent.py maya|jordan|sam|alex')
name = sys.argv[1]
persona = load_persona(name, BASE)
calendar = google_calendar.CalendarClient(BASE)
PLAN_TIMEZONE = os.getenv('PLAN_TIMEZONE', 'America/Los_Angeles')


def tiger(fn, *args):
    """Run a memory.py call on Tiger Postgres. Returns None when DATABASE_URL is unset or the
    database fails: group memory is optional and must never block planning."""
    if not os.getenv("DATABASE_URL"):
        return None
    try:
        import memory
        with memory.connect() as conn:
            return fn(conn, *args)
    except Exception as exc:
        print(f"{name}: Tiger Postgres unavailable ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return None


def remember_persona(p):
    import memory
    tiger(memory.save_persona, name, {k: p[k] for k in ("hard", "soft", "weights", "cuisine_pref") if k in p})


def load_from_tiger():
    import memory
    # Seed only once: four agents starting together would otherwise collide on the same rows.
    tiger(lambda conn: (memory.init(conn), memory.group_exists(conn, GROUP_ID) or memory.seed_demo(conn, BASE)))
    return tiger(memory.get_persona, name)


# Preferences saved in Tiger win over the local file, so a fresh server keeps them.
persona = {**persona, **(load_from_tiger() or {})}
port = persona['port']
agent = Agent(name=f'consensusai-{name}', seed=persona['seed'], port=port, mailbox=True)
chat = Protocol(spec=chat_protocol_spec)

async def reply(ctx, target, message):
    ctx.logger.info(
        "SENDING CHAT to=%s preview=%r",
        target,
        message[:150]
    )

    try:
        result = await ctx.send(
            target,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(type="text", text=message)
                ]
            )
        )

        ctx.logger.info(
            "CHAT SEND RESULT: %s",
            result
        )

    except Exception:
        ctx.logger.exception(
            "FAILED to send chat message to %s",
            target
        )
def get_candidates(round_no):
    if os.getenv("NEGOTIATION_DEMO") == "1":
        venue = next(
            v for v in venues
            if v["id"] == "med-grill"
        )

        slot = "2026-10-25T18:00"

        return [
            Candidate(
                id=f"{venue['id']}@{slot}",
                name=venue["name"],
                cuisine=venue["cuisine"],
                price=venue["price"],
                distance_min=venue["distance_min"],
                rating=venue["rating"],
                tags=venue["tags"],
                slot=slot,
            )
        ]

    # Normal mode: every venue at every slot for this round, same as organizer.candidates().
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


async def find_live_candidates(ctx):
    """Google Places venues verified open at every slot -> (candidates, note for the user).
    candidates is None when the plan should use venues.json instead."""
    if os.getenv("NEGOTIATION_DEMO") == "1" or not os.getenv("GOOGLE_PLACES_API_KEY"):
        return None, None

    try:
        results = await asyncio.gather(*(
            asyncio.to_thread(places.search, PLAN_AREA, slot)
            for slot in SLOTS[2]
        ))
    except Exception as exc:
        # Places down or out of quota: never invent venues; fall back and say so.
        ctx.logger.warning("Google Places search failed: %r", exc)
        return None, (
            f"Live restaurant search failed ({type(exc).__name__}), "
            "so this plan uses the demo restaurant list."
        )

    found = [c for kept, _ in results for c in kept]

    if not found:
        return None, (
            f"No restaurants near {PLAN_AREA} are verified open at the "
            "planned times, so this plan uses the demo restaurant list."
        )

    for c in found:
        maps_links[c.id] = c.maps_url
        reservable[c.id] = c.reservable

    candidates = [
        Candidate(
            id=c.id,
            name=c.name,
            cuisine=c.cuisine,
            price=c.price,
            distance_min=c.distance_min,
            rating=c.rating,
            tags=c.tags,
            slot=c.slot,
        )
        for c in found
    ]

    return candidates, (
        f"Found {len({c.name for c in found})} restaurants near "
        f"{PLAN_AREA.split(',')[0]} on Google, each verified open "
        "at the planned time."
    )


def round_candidates(plan):
    # Live plans search every slot once up front; each round takes its slots from that pool,
    # so the re-check after an approved counteroffer scores the exact same venues.
    if plan.live is None:
        return get_candidates(plan.round)

    return [c for c in plan.live if c.slot in SLOTS[plan.round]]


async def begin_round(ctx, plan):
    plan.begin_round(round_candidates(plan))

    request = plan.make_request()

    # IMPORTANT:
    # The coordinating agent participates in the decision.
    # Score candidates using its own private preferences.
    # Pause the reply timer while checking the coordinator's own calendar,
    # before peers have even received their scoring requests.
    plan.finalizing = True
    own_scores = await score_with_calendar(ctx, plan.candidates)
    plan.t0 = time.monotonic()
    plan.finalizing = False

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

def use_plan_session(ctx, plan):
    # ASI:One threads chat by session. A handler triggered by a peer or a timer
    # would otherwise reply under that trigger's session, outside the user's chat.
    ctx._session = plan.session  # uagents' ctx.send() takes no session argument


async def end_negotiation(ctx, plan_id, message):
    negotiation = negotiations.pop(plan_id, None)
    plan = plans.pop(plan_id, None)

    if plan is None:
        return

    use_plan_session(ctx, plan)
    latest_status[plan.user] = message
    await reply(ctx, plan.user, message)

async def finalize_peer_plan(ctx, plan):
    # Prevent duplicate finalization.
    if plan.finalizing:
        return

    plan.finalizing = True
    use_plan_session(ctx, plan)

    missing = plan.missing_agents()

    ctx.logger.info(
        "FINALIZING plan=%s missing=%s recipient=%s",
        plan.id,
        missing,
        plan.user,
    )

    # --------------------------------------------------
    # CASE 1: One or more agents did not respond.
    # --------------------------------------------------
    if missing:
        message = (
            f"INCOMPLETE: Plan {plan.id}. "
            f"Missing responses from {', '.join(missing)}. "
            "No group decision was made."
        )

        plans.pop(plan.id, None)
        latest_status[plan.user] = message

        await reply(ctx, plan.user, message)
        return

    # --------------------------------------------------
    # CASE 2: Validate the collected scores.
    # --------------------------------------------------
    table = build_complete_table(
        plan.replies,
        plan.candidates,
        plan.participants,
    )

    if table is None:
        message = (
            f"INCOMPLETE: Plan {plan.id}. "
            "Invalid or partial agent scores. "
            "No group decision was made."
        )

        plans.pop(plan.id, None)
        latest_status[plan.user] = message

        await reply(ctx, plan.user, message)
        return

    # --------------------------------------------------
    # CASE 3: No feasible restaurant.
    # --------------------------------------------------
    if not table:

        # First, try a wider date search.
        if plan.round == 1:
            plan.round = 2

            await reply(
                ctx,
                plan.user,
                "All candidates were vetoed. "
                "Trying an additional date without "
                "relaxing deal breakers.",
            )

            await begin_round(ctx, plan)
            return

        # Do not repeatedly negotiate the same plan.
        # A completed approval triggers one fresh scoring pass.
        if getattr(plan, "negotiation_attempted", False):
            message = (
                f"NO FEASIBLE PLAN: {plan.id}. "
                "The group still has no unanimously feasible "
                "option after re-evaluation. "
                "No restaurant was selected."
            )

            negotiations.pop(plan.id, None)
            plans.pop(plan.id, None)
            latest_status[plan.user] = message

            await reply(ctx, plan.user, message)
            return

        # Find candidates vetoed by the fewest people.
        misses = near_misses(plan.replies, limit=3)

        # For this first implementation, negotiate only
        # a candidate blocked by exactly one person.
        single_blocker_ids = {
            miss.candidate_id
            for miss in misses
            if len(miss.blockers) == 1
        }

        offers = counteroffers(
            plan.replies,
            plan.candidates,
            limit=3,
        )

        offer = next(
            (
                item
                for item in offers
                if item.candidate_id in single_blocker_ids
                and item.person != name
            ),
            None,
        )

        if offer is None:
            message = (
                f"NO FEASIBLE PLAN: {plan.id}. "
                "No suitable single-person counteroffer "
                "was found. No constraints were relaxed "
                "and no group decision was made."
            )

            plans.pop(plan.id, None)
            latest_status[plan.user] = message

            await reply(ctx, plan.user, message)
            return

        candidate = next(
            (
                c for c in plan.candidates
                if c.id == offer.candidate_id
            ),
            None,
        )

        if candidate is None:
            message = (
                f"NEGOTIATION ERROR: Plan {plan.id}. "
                "The proposed restaurant could not be found."
            )

            plans.pop(plan.id, None)
            latest_status[plan.user] = message

            await reply(ctx, plan.user, message)
            return

        # Keep the session alive while awaiting
        # approval from the affected person's agent.
        negotiations[plan.id] = {
            "person": offer.person,
            "candidate_id": candidate.id,
            "started": time.monotonic(),
            "status": "requested",
        }

        # Hold the plan open; it is no longer
        # waiting for ordinary scoring responses.
        ctx.logger.info(
            "NEGOTIATING plan=%s participant=%s candidate=%s",
            plan.id,
            offer.person,
            candidate.id,
        )

        try:
            await ctx.send(
                ADDR[offer.person],
                CounterofferRequest(
                    plan_id=plan.id,
                    candidate=candidate,
                    category=offer.category,
                ),
            )

        except Exception:
            ctx.logger.exception(
                "Failed to send counteroffer for plan %s",
                plan.id,
            )

            await end_negotiation(
                ctx,
                plan.id,
                f"NEGOTIATION FAILED: Plan {plan.id}. "
                "The counteroffer could not be sent.",
            )
            return

        message = (
            f"NEGOTIATION STARTED: Plan {plan.id}.\n"
            f"No restaurant satisfies everyone's "
            f"current hard constraints.\n"
            f"Asked {offer.person}'s personal agent "
            f"to review a private counteroffer.\n"
            "Waiting for a response. "
            "No preferences have been changed by the coordinator."
        )

        latest_status[plan.user] = message

        await reply(ctx, plan.user, message)
        return

    # --------------------------------------------------
    # CASE 4: At least one feasible restaurant exists.
    # --------------------------------------------------
    fair, average_choice = choose(table)

    # Generate compromise alternatives if the selected
    # option leaves someone significantly unhappy.
    compromise = propose_compromise(table)

    # Look the venue up in this plan's candidates: live ones are not in venues.json.
    chosen = next(c for c in plan.candidates if c.id == fair)
    slot = chosen.slot
    last_plans[plan.user] = {"id": fair, "venue": chosen.name, "slot": slot, "size": len(plan.participants)}
    import memory
    await asyncio.to_thread(tiger, memory.record_plan, plan.id, GROUP_ID, fairness.decide(table))

    scores = table[fair]

    lowest_person = min(
        scores,
        key=scores.get,
    )

    average_score = (
        sum(scores.values()) / len(scores)
    )

    lines = [
        f"PROPOSED GROUP PLAN (coordinated by {name})",
        f"Plan ID: {plan.id}",
        f"Restaurant: {chosen.name}",
        f"Cuisine: {chosen.cuisine}",
        f"Time: {slot}",
        f"Price: approximately ${chosen.price} per person",
        "",
        f"Minimum satisfaction: {lowest_person} = "
        f"{scores[lowest_person]:.2f}",
        f"Average satisfaction: {average_score:.2f}",
    ]

    # Explain how fairness differs from
    # maximizing average satisfaction.
    if average_choice != fair:
        alternative = table[average_choice]
        weakest = min(
            alternative,
            key=alternative.get,
        )

        lines.append(
            "Highest-average alternative would leave "
            f"{weakest} at {alternative[weakest]:.2f}."
        )

    # --------------------------------------------------
    # CASE 5: Show alternative compromises if needed.
    # --------------------------------------------------
    if compromise is not None:
        lines.append("")
        lines.append("Alternative group compromises:")

        for option in compromise.options:
            lines.append(option.line)

        lines.append(
            "These alternatives are suggestions, "
            "not approved decisions."
        )

    # --------------------------------------------------
    # CASE 6: Generate calendar draft.
    # --------------------------------------------------
    lines.append("")
    if maps_links.get(fair):
        lines.append("Verified open on Google Maps: " + maps_links[fair])
    lines.append(
        "Calendar draft: "
        + calendar_link(
            f"Dinner at {chosen.name}",
            slot,
            chosen.name,
        )
    )

    lines.append(
        "This is a proposal, not a booking. "
        "All participants must approve before "
        "any reservation or payment."
    )

    # Finish the planning session.
    message = "\n".join(lines)

    negotiations.pop(plan.id, None)
    plans.pop(plan.id, None)

    latest_status[plan.user] = message

    await reply(ctx, plan.user, message)

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
    if not text:
        return

    valid_commands = {
        "plan",
        "plan dinner",
        "help",
        "start",
        "show",
        "confirm",
        "cancel",
        "status",
        "offer",
        "book",
        "calendar",
        "calendar status",
        "calendar disconnect",
    }

    lower_text = text.lower()

    # This is NOT real authentication. For a public demo, configure a private allowlist.
    allowed = [x.strip() for x in os.getenv(f'{name.upper()}_OWNER_ADDRESSES','').split(',') if x.strip()]

    # Other free text from the owner is a plain-language preference (StepFun, below).
    if (
        lower_text not in valid_commands
        and not lower_text.startswith("set ")
        and not lower_text.startswith("approve ")
        and not lower_text.startswith("reject ")
        and sender not in allowed
    ):
        ctx.logger.info(
            "Ignoring unrecognized message: %.100r",
            text,
        )
        return
    if not allowed or sender not in allowed:
        await reply(ctx, sender, 'Preference edits disabled. Set '+name.upper()+'_OWNER_ADDRESSES to the allowed ASI:One sender address. Ask the agent operator to configure it.')
        return
    if lower_text in ('calendar', 'calendar status'):
        if calendar.connected(name):
            message = ('Google Calendar is connected locally. Planning checks your primary calendar '
                       'for two-hour conflicts; authorization is verified during the check. '
                       'Send calendar disconnect to revoke access.')
        else:
            message = (f'Google Calendar is not connected. On your computer, run '
                       f'python google_calendar.py connect {name} --credentials data/google-oauth-client.json '
                       'and sign in to Google. Setup: docs/google-calendar.md. '
                       'Planning currently uses your manually entered availability.')
        await reply(ctx, sender, message)
        return
    if lower_text == 'calendar disconnect':
        try:
            await asyncio.to_thread(calendar.disconnect, name)
            await reply(ctx, sender, 'Google Calendar disconnected. Planning uses manual availability again.')
        except google_calendar.CalendarError as exc:
            await reply(ctx, sender, str(exc))
        return
        # A personal agent can initiate a group planning session.
    
        # Allow the user to retrieve the latest planning result
    # -------------------------------------------------
    # Private negotiation commands
    # -------------------------------------------------

    if lower_text == "offer":
        active = [
            (plan_id, offer)
            for plan_id, offer in pending_counteroffers.items()
            if offer["owner"] == sender
        ]

        if not active:
            await reply(ctx, sender, "No pending counteroffers.")
            return

        lines = []

        for plan_id, offer in active:
            explanation = describe_relaxation(
                persona,
                offer["changes"],
            )

            lines.append(
                f"Plan {plan_id}: "
                f"{offer['candidate'].name}\n"
                f"Proposed change: {explanation}\n"
                f"APPROVE {plan_id} or REJECT {plan_id}"
            )

        await reply(ctx, sender, "\n\n".join(lines))
        return

    if lower_text.startswith(("approve ", "reject ")):
        parts = lower_text.split()

        if len(parts) != 2:
            await reply(
                ctx,
                sender,
                "Use APPROVE <plan_id> or REJECT <plan_id>.",
            )
            return

        action, plan_id = parts
        offer = pending_counteroffers.get(plan_id)

        if not offer or offer["owner"] != sender:
            await reply(
                ctx,
                sender,
                "No matching pending counteroffer.",
            )
            return

        if (
            time.monotonic() - offer["created_at"]
            >= NEGOTIATION_TIMEOUT
        ):
            pending_counteroffers.pop(plan_id, None)

            await ctx.send(
                offer["coordinator"],
                CounterofferReply(
                    plan_id=plan_id,
                    persona=name,
                    candidate_id=offer["candidate"].id,
                    status="rejected",
                ),
            )

            await reply(
                ctx,
                sender,
                "This counteroffer has expired.",
            )
            return

        if action == "reject":
            pending_counteroffers.pop(plan_id, None)

            await ctx.send(
                offer["coordinator"],
                CounterofferReply(
                    plan_id=plan_id,
                    persona=name,
                    candidate_id=offer["candidate"].id,
                    status="rejected",
                ),
            )

            await reply(
                ctx,
                sender,
                "Counteroffer rejected. "
                "Your preferences were not changed.",
            )
            return

        # Explicit human approval.
        # Validate the update again before saving.
        try:
            updated = validate_update(
                persona,
                offer["changes"],
            )

            save_persona(name, BASE, updated)
            persona = updated
            await asyncio.to_thread(remember_persona, updated)

        except (ValueError, TypeError) as exc:
            await reply(
                ctx,
                sender,
                f"Could not apply counteroffer: {exc}",
            )
            return

        pending_counteroffers.pop(plan_id, None)

        await ctx.send(
            offer["coordinator"],
            CounterofferReply(
                plan_id=plan_id,
                persona=name,
                candidate_id=offer["candidate"].id,
                status="approved",
            ),
        )

        await reply(
            ctx,
            sender,
            f"Approved compromise for plan {plan_id}. "
            "Your updated preferences have been saved.",
        )
        return
    if lower_text == "book":
        target = last_plans.get(sender)
        if not target:
            await reply(ctx, sender, "No plan to book yet. Send plan first.")
            return
        when = target["slot"].replace("T", " at ")
        if reservable.get(target["id"]) is False:
            # Google says walk-in only: nothing to reserve, so don't spend a Browser Use check.
            await reply(ctx, sender, f"{target['venue']} doesn't take reservations: just walk in on {when}.")
            return
        await reply(ctx, sender, f"Checking reservations for {target['size']} at {target['venue']}, {when}... (usually 1-3 minutes)")
        result = await asyncio.to_thread(
            booking.check_availability, target["venue"], target["slot"], target["size"],
            PLAN_AREA.split(",")[-1].strip() or "Los Angeles")
        await reply(ctx, sender, booking.message(result))
        return
    if text.lower() == "status":
        message = latest_status.get(
            sender,
            "No completed planning session found."
        )

        await reply(ctx, sender, message)
        return
    if text.lower() in ("plan", "plan dinner"):
        if any(p.user == sender for p in plans.values()):
            await reply(
                ctx,
                sender,
                "You already have an active planning session.",
            )
            return

        # Search before the plan exists, so the search can't eat into the scoring timeout.
        live, note = await find_live_candidates(ctx)

        plan = Plan(
            user=sender,
            participants=ADDR.copy(),
            coordinator=agent.address,
        )
        plan.session = ctx.session  # the user's ASI:One chat; see use_plan_session()
        plan.live = live

        plans[plan.id] = plan

        await reply(
            ctx,
            sender,
            (note + "\n" if note else "")
            + f"{name.title()} is coordinating plan {plan.id}. "
            f"All {len(ADDR)} personal agents will participate.",
        )

        await begin_round(ctx, plan)
        return
    if text.lower() in ('help', 'start'):
        await reply(ctx, sender, 'Tell me a preference in plain words, e.g. "I\'m vegetarian now and want to stay under $30", or use commands: show | set max_price 25 | set max_distance_min 20 | set dietary vegetarian,halal | set free_slots 2026-10-23T19:00,2026-10-24T19:00 | set weights {"cuisine":4,"rating":3,"distance":2,"price":1} | set cuisine_pref {"thai":1,"italian":0.5}. Send confirm before the update is applied. Calendar: calendar status | calendar disconnect. After a plan: book checks for a real table (never books). Do preference changes in a private chat with me: in a group chat everyone sees my replies.')
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
                await asyncio.to_thread(remember_persona, updated)
                ctx.storage.set(f'pending_{sender}','')
                await reply(ctx, sender, 'Saved. Updated preference: '+pending)
            except (ValueError,TypeError) as e:
                await reply(ctx,sender,'Cannot apply update: '+str(e))
    elif text.lower() == 'cancel':
        ctx.storage.set(f'pending_{sender}','')
        await reply(ctx,sender,'Pending preference update cancelled.')
    else:
        # `set ...` is parsed directly; plain text goes to StepFun. Either way the result is only
        # a proposal: validate_update checks it again and nothing saves without `confirm`.
        proposal = await asyncio.to_thread(nl_preferences.propose, text, persona)
        if not proposal.update:
            problem = '; '.join(proposal.rejected[:2]) or "No change: that already matches your preferences, or I didn't catch one."
            await reply(ctx, sender, problem + '\nSend help for examples.')
            return
        ctx.storage.set(f'pending_{sender}', json.dumps(proposal.update))
        await reply(ctx, sender, 'Proposed change:\n' + proposal.summary + '\nReply confirm to save, or cancel.')

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
    scores = await score_with_calendar(ctx, msg.candidates)
    await ctx.send(sender,ScoreReply(plan_id=msg.plan_id,round=msg.round,persona=name,scores=scores))


async def score_with_calendar(ctx, candidates):
    """Use the same private availability checks for coordinator and peer scoring."""
    try:
        evaluations = await asyncio.to_thread(
            google_calendar.evaluate_candidates, name, persona, candidates,
            client=calendar, timezone_name=PLAN_TIMEZONE,
        )
        return [CandidateScore(candidate_id=c.id, veto=veto, score=score, reason=reason)
                for c, (veto, score, reason) in zip(candidates, evaluations)]
    except google_calendar.CalendarError as exc:
        # An empty reply makes the coordinator report INCOMPLETE. A failed
        # calendar request must never be mistaken for an empty (free) calendar.
        ctx.logger.warning('Calendar check failed: %s', exc)
        return []

@agent.on_message(CounterofferRequest)
async def on_counteroffer_request(
    ctx: Context,
    sender: str,
    msg: CounterofferRequest,
):
    # Only accept negotiation requests from known personal agents.
    if sender not in ADDR.values() or sender == agent.address:
        ctx.logger.warning(
            "Rejected unauthorized counteroffer from %s",
            sender,
        )
        return

    # Avoid replacing an existing pending approval.
    if pending_counteroffers:
        await ctx.send(
            sender,
            CounterofferReply(
                plan_id=msg.plan_id,
                persona=name,
                candidate_id=msg.candidate.id,
                status="rejected",
            ),
        )
        return

    # A manual preference exception cannot erase a real calendar conflict.
    try:
        busy = await asyncio.to_thread(
            calendar.unavailable_slots, name, [msg.candidate.slot], timezone_name=PLAN_TIMEZONE,
        ) if calendar.connected(name) else set()
    except google_calendar.CalendarError:
        busy = {msg.candidate.slot}  # Unverified availability cannot be negotiated away.
    if busy:
        await ctx.send(sender, CounterofferReply(
            plan_id=msg.plan_id, persona=name, candidate_id=msg.candidate.id, status='not_possible',
        ))
        return

    # Calculate the smallest possible relaxation privately.
    changes = minimal_relaxation(persona, msg.candidate)

    if changes is None:
        await ctx.send(
            sender,
            CounterofferReply(
                plan_id=msg.plan_id,
                persona=name,
                candidate_id=msg.candidate.id,
                status="not_possible",
            ),
        )
        return

    if not changes:
        await ctx.send(
            sender,
            CounterofferReply(
                plan_id=msg.plan_id,
                persona=name,
                candidate_id=msg.candidate.id,
                status="already_feasible",
            ),
        )
        return

    # Private approval requires one unambiguous owner.
    owners = [
        address.strip()
        for address in os.getenv(
            f"{name.upper()}_OWNER_ADDRESSES", ""
        ).split(",")
        if address.strip()
    ]

    if len(owners) != 1:
        await ctx.send(
            sender,
            CounterofferReply(
                plan_id=msg.plan_id,
                persona=name,
                candidate_id=msg.candidate.id,
                status="not_possible",
            ),
        )
        return

    explanation = describe_relaxation(persona, changes)

    pending_counteroffers[msg.plan_id] = {
        "coordinator": sender,
        "candidate": msg.candidate,
        "changes": changes,
        "owner": owners[0],
        "created_at": time.monotonic(),
    }

    private_message = (
        f"PRIVATE COUNTEROFFER\n"
        f"Plan: {msg.plan_id}\n"
        f"Restaurant: {msg.candidate.name}\n"
        f"Time: {msg.candidate.slot}\n\n"
        f"Proposed compromise: {explanation}\n\n"
        f"Reply APPROVE {msg.plan_id} to accept, "
        f"or REJECT {msg.plan_id} to decline.\n"
        f"No changes have been made."
    )

    # Send the explanation only to the personal agent's owner.
    await reply(ctx, owners[0], private_message)

    # The coordinator learns only that approval is required.
    await ctx.send(
        sender,
        CounterofferReply(
            plan_id=msg.plan_id,
            persona=name,
            candidate_id=msg.candidate.id,
            status="approval_required",
        ),
    )

@agent.on_message(CounterofferReply)
async def on_counteroffer_reply(
    ctx: Context,
    sender: str,
    msg: CounterofferReply,
):
    negotiation = negotiations.get(msg.plan_id)
    plan = plans.get(msg.plan_id)

    if not negotiation or not plan:
        return

    use_plan_session(ctx, plan)

    # Verify the expected personal agent sent this response.
    if sender != ADDR.get(negotiation["person"]):
        ctx.logger.warning(
            "Rejected unexpected counteroffer reply from %s",
            sender,
        )
        return

    if (
        msg.persona != negotiation["person"]
        or msg.candidate_id != negotiation["candidate_id"]
    ):
        return

    if msg.status == "approval_required":
        negotiation["status"] = "awaiting_human"
        ctx.logger.info(
            "Plan %s awaiting human approval from %s",
            plan.id,
            msg.persona,
        )
        return

    if msg.status in ("rejected", "not_possible"):
        await end_negotiation(
            ctx,
            plan.id,
            f"NEGOTIATION ENDED: Plan {plan.id}. "
            f"{msg.persona} could not approve the proposed compromise. "
            "No group decision was made.",
        )
        return

    if msg.status in ("approved", "already_feasible"):
        # A fresh scoring round must use everyone's current preferences.
        negotiations.pop(plan.id, None)

        # Increment the round and reuse the last candidate set.
        # The approved preference is now saved by the personal agent.
        # Re-evaluate the existing round-2 candidate set.
        plan.negotiation_attempted = True
        await begin_round(ctx, plan)

        await reply(
            ctx,
            plan.user,
            f"{msg.persona}'s agent accepted the proposed "
            "compromise. Rechecking all participants' scores.",
        )


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
    # Expire coordinator-side negotiations.
    for plan_id, negotiation in list(negotiations.items()):
        if (
            time.monotonic() - negotiation["started"]
            >= NEGOTIATION_TIMEOUT
        ):
            await end_negotiation(
                ctx,
                plan_id,
                f"NEGOTIATION TIMED OUT: Plan {plan_id}. "
                "No group decision was made.",
            )

    # Expire private pending approvals.
    for plan_id, offer in list(pending_counteroffers.items()):
        if (
            time.monotonic() - offer["created_at"]
            >= NEGOTIATION_TIMEOUT
        ):
            pending_counteroffers.pop(plan_id, None)

            await ctx.send(
                offer["coordinator"],
                CounterofferReply(
                    plan_id=plan_id,
                    persona=name,
                    candidate_id=offer["candidate"].id,
                    status="rejected",
                ),
            )

    # Ordinary scoring timeout.
    for plan in list(plans.values()):
        if plan.id in negotiations:
            continue

        if not plan.finalizing and plan.expired(TIMEOUT):
            ctx.logger.warning(
                "TIMEOUT plan=%s missing=%s",
                plan.id,
                plan.missing_agents(),
            )
            await finalize_peer_plan(ctx, plan)
agent.include(chat,publish_manifest=True)
if __name__=='__main__':
    agent.run()
