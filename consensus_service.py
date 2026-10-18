"""ConsensusAI over a chat channel (iMessage via Photon today). One person per thread.

Each sender is a member with a private persona: this service is their personal agent.
Their deal breakers never leave it; other members only learn veto categories and the plan.

  start group / join CODE     form a group (friends text the agent themselves)
  plan dinner sat 7pm near X  live Google Places venues, verified open -> every member's
                              persona scores privately -> leximin fair pick -> sent to all
  book                        Browser Use checks a real table for the group (never books)
  busy                        you can't make the plan's day: saved, and the group is told
  anything else               StepFun proposes a preference change; only your YES saves it

handle() is a generator of (contact, text) so slow steps (booking) can say "checking..." first.
"""
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import booking
import fairness
import nl_preferences
import venues
from negotiation import when
from preferences import validate_update
from scoring import evaluate

DEFAULT_PERSONA = {'hard': {'max_price': 60, 'max_distance_min': 30, 'dietary': [], 'busy_days': []},
                   'cuisine_pref': {}, 'weights': {'cuisine': 1, 'rating': 1, 'distance': 1, 'price': 1}}
DEFAULT_AREA = 'Westwood, Los Angeles'
BOOK_TRIES = 2  # top pick + runner-up; each check takes 1-3 minutes
OPTIONS_KEPT = 5  # ranked venues kept per plan, so walk-in places can be skipped
CODE_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no 0/O/1/I
DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
HELP = ("ConsensusAI plans group outings fairly, and keeps your deal breakers private.\n"
        "- Tell me what you like: \"I'm vegetarian, max $30, I love thai\"\n"
        "- START GROUP, then friends text JOIN <code>\n"
        "- PLAN dinner sat 7pm near Westwood\n"
        "- BOOK to check a real table, BUSY if you can't make it\n"
        "- SHOW your preferences, NAME <your name>")


@dataclass
class Store:
    """Thin layer over memory.py with one autocommit connection that reconnects once if dropped."""
    url: str

    def __post_init__(self):
        import memory
        self.memory, self.conn = memory, None

    def __getattr__(self, name):
        fn = getattr(self.memory, name)

        def call(*args):
            for attempt in (1, 2):
                try:
                    if self.conn is None or self.conn.closed:
                        self.conn = self.memory.connect(self.url)
                        self.conn.autocommit = True
                    return fn(self.conn, *args)
                except self.memory.psycopg.OperationalError:
                    self.conn = None
                    if attempt == 2:
                        raise
        return call


def parse_when(text, now):
    """Next matching day at the stated time; defaults to Saturday 7pm."""
    t = text.lower()
    m = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b', t)
    if m:
        hour, minute = int(m.group(1) or m.group(4)), int(m.group(2) or m.group(5) or 0)
        hour = hour % 12 + (12 if m.group(3) == 'pm' else 0) if m.group(3) else hour
    else:
        hour, minute = 19, 0
    day = next((i for i, d in enumerate(DAYS) if re.search(rf'\b{d[:3]}(?:{d[3:]})?\b', t)), None)
    if 'tomorrow' in t:
        date = now.date() + timedelta(days=1)
    elif 'today' in t or 'tonight' in t:
        date = now.date()
    else:
        target = 5 if day is None else day
        date = now.date() + timedelta(days=(target - now.weekday()) % 7)
    dt = datetime(date.year, date.month, date.day, hour, minute)
    return (dt + timedelta(days=7) if dt < now else dt).strftime('%Y-%m-%dT%H:%M')


def parse_area(text):
    m = re.search(r'\b(?:near|in|around)\s+([a-z0-9 .,\'-]+?)\s*[.!?]*$', text, re.I)
    area = m.group(1).strip() if m else ''
    if not area or re.fullmatch(r'(the )?(area|town|city)', area, re.I):
        return DEFAULT_AREA
    return area if ',' in area or 'los angeles' in area.lower() else f'{area}, Los Angeles'


class Service:
    def __init__(self, store, llm=nl_preferences.stepfun_chat, places=venues.search,
                 check_table=booking.check_availability, now=datetime.now, rng=random.Random()):
        self.store, self.llm, self.places, self.check_table, self.now, self.rng = store, llm, places, check_table, now, rng
        self.pending = {}   # member -> proposed update. shortcut: in-process; a restart drops unconfirmed proposals.
        self.plans = {}     # group -> last plan context for BOOK / BUSY
        self.walk_ins = set()  # venues a booking check found take no reservations (this process)

    # ---- helpers ----
    def name(self, member):
        return self.store.member_name(member) or f'Friend {member[-2:]}'

    def persona(self, member):
        return self.store.get_persona(member) or DEFAULT_PERSONA

    def group_or_solo(self, member):
        g = self.store.member_group(member)
        return g, (self.store.group_members(g) if g else [member])

    def to_all(self, members, text, contact, sender):
        for m in members:
            c = contact if m == sender else self.store.get_contact(m)
            if c:
                yield c, text

    # ---- entry point ----
    def handle(self, member, text, contact):
        new = self.store.get_persona(member) is None
        self.store.add_member(member, self.store.member_name(member) or f'Friend {member[-2:]}')
        self.store.save_contact(member, contact['channel'], contact['space_id'], contact['agent_id'])
        if new:
            self.store.save_persona(member, DEFAULT_PERSONA)
        cmd = text.strip().lower()
        word = cmd.split()[0] if cmd else ''
        if new and word not in ('join', 'plan'):
            yield contact, 'Welcome to ConsensusAI! ' + HELP
            if cmd in ('hi', 'hello', 'hey', 'help', 'start', ''):
                return
        if cmd in ('help', '?', 'hi', 'hello', 'hey'):
            yield contact, HELP
        elif cmd in ('yes', 'y', 'confirm', 'save'):
            yield contact, self.confirm(member)
        elif cmd in ('no', 'n', 'cancel'):
            yield contact, 'Discarded.' if self.pending.pop(member, None) else 'Nothing pending.'
        elif cmd in ('show', 'my preferences', 'preferences', 'prefs'):
            yield contact, self.show(member)
        elif word == 'name' and len(cmd.split()) > 1:
            name = text.strip()[5:].strip()[:40]
            self.store.add_member(member, name)
            yield contact, f'Got it, {name}.'
        elif cmd in ('start group', 'new group', 'create group'):
            yield contact, self.start_group(member)
        elif word == 'join':
            yield from self.join(member, cmd.split()[1].upper() if len(cmd.split()) > 1 else '', contact)
        elif word == 'plan':
            yield from self.plan(member, text, contact)
        elif cmd in ('book', 'book it', 'reserve'):
            yield from self.book(member, contact)
        elif cmd in ('busy', "can't make it", 'cant make it', "can't", 'cant'):
            yield from self.busy(member, contact)
        else:
            yield contact, self.propose(member, text)

    # ---- preferences (owner only) ----
    def upcoming(self):
        today = self.now().date()
        days = [(today + timedelta(days=i)).isoformat() for i in range(14)]
        return [f'{d}T19:00' for d in days[:7]], days

    def propose(self, member, text):
        slots, days = self.upcoming()
        p = nl_preferences.propose(text, self.persona(member), self.llm, slots, days)
        if p.update:
            self.pending[member] = p.update
            return f'I would update your private preferences:\n{p.summary}\nReply YES to save or NO to discard.'
        if p.rejected:
            return "I couldn't use that: " + '; '.join(p.rejected[:2]) + '\nText HELP for examples.'
        return "I didn't catch a preference there. Text HELP for examples."

    def confirm(self, member):
        update = self.pending.pop(member, None)
        if not update:
            return 'Nothing pending. Tell me a preference first.'
        self.store.save_persona(member, validate_update(self.persona(member), update))
        return 'Saved. Only your agent can see these.'

    def show(self, member):
        p = self.persona(member)
        h = p['hard']
        likes = ', '.join(f'{c} {v:.1f}' for c, v in sorted(p['cuisine_pref'].items(), key=lambda kv: -kv[1])) or 'none yet'
        return (f"Your private preferences:\n- budget up to ${h['max_price']:g}, travel up to {h['max_distance_min']:g} min\n"
                f"- dietary: {', '.join(h['dietary']) or 'none'}; never: {', '.join(h.get('never_cuisines', [])) or 'none'}\n"
                f"- busy: {', '.join(h.get('busy_days', [])) or 'none'}\n- cuisines: {likes}")

    # ---- groups ----
    def start_group(self, member):
        code = ''.join(self.rng.choice(CODE_CHARS) for _ in range(5))
        while self.store.group_exists(code):
            code = ''.join(self.rng.choice(CODE_CHARS) for _ in range(5))
        self.store.create_group(code, f'Group {code}', [member])
        return f'Group {code} created. Friends text this number: JOIN {code}'

    def join(self, member, code, contact):
        if not code or not self.store.group_exists(code):
            yield contact, f"I can't find group {code or '?'}. Check the code and text JOIN <code>."
            return
        self.store.create_group(code, f'Group {code}', [member])
        members = self.store.group_members(code)
        yield contact, f'You joined group {code} ({len(members)} people). Tell me your preferences anytime.'
        yield from self.to_all([m for m in members if m != member], f'{self.name(member)} joined group {code}.', contact, member)

    # ---- planning ----
    def plan(self, member, text, contact):
        group, members = self.group_or_solo(member)
        slot, area = parse_when(text, self.now()), parse_area(text)
        try:
            candidates, dropped = self.places(area, slot)
        except Exception as e:  # Places down: say so, never invent venues
            yield contact, f"I couldn't search restaurants right now ({type(e).__name__}). Try again in a minute."
            return
        if not candidates:
            yield contact, f'No restaurants near {area} are verified open at {when(slot)}. Try another time or area.'
            return
        personas = {m: self.persona(m) for m in members}
        scores = {m: {c.id: evaluate(personas[m], c) for c in candidates} for m in members}
        table = {c.id: {m: scores[m][c.id][1] for m in members} for c in candidates
                 if not any(scores[m][c.id][0] for m in members)}
        if not table:
            yield from self.no_plan(member, members, scores, candidates, slot, contact)
            return
        by_id = {c.id: c for c in candidates}
        history = self.store.get_credits(group) if group else None
        d = fairness.decide(table, history=history, names={cid: by_id[cid].name for cid in table})
        c = by_id[d.chosen]
        if group:
            self.store.record_plan(f'{group}-{self.rng.randrange(16**6):06x}', group, d)
        self.plans[group or member] = {'venue': c.name, 'slot': slot, 'size': len(members), 'area': area,
                                       'options': [(by_id[cid].name, self.reservable(by_id[cid]))
                                                   for cid in d.ranking[:OPTIONS_KEPT]]}
        who = f'the {len(members)} of you' if len(members) > 1 else 'you'
        res = {True: ', takes reservations', False: ', walk-in only'}.get(self.reservable(c), '')
        base = (f"Plan for {who}: {c.name} ({c.cuisine}, {c.rating:.1f} stars, about ${c.price} per person{res}), "
                f"{when(slot)} near {area.split(',')[0]}.\n{d.explanation}\n"
                f"Verified open on Google just now ({len(dropped)} closed or unverified places skipped).")
        tail = ('\nReply BOOK to check a real table, or BUSY if you cannot make it.' if group
                else '\nReply BOOK to check a table. START GROUP to plan with friends.')
        for m in members:
            target = contact if m == member else self.store.get_contact(m)
            if target:
                yield target, f'{base}\nYour match: {table[d.chosen][m]:.0%}.{tail}'

    def no_plan(self, member, members, scores, candidates, slot, contact):
        # Every candidate shares the slot, so one availability veto means that person is busy then.
        busy = [m for m in members if any(s[0] and s[2] == 'not free at that time' for s in scores[m].values())]
        if busy:
            names = ', '.join(self.name(m) for m in busy)
            yield contact, f"{names} can't make {when(slot)}. Try another day, e.g. PLAN dinner sunday 7pm."
            return
        blocked = {}
        for c in candidates:  # the closest venue: blocked by the fewest people
            reasons = [scores[m][c.id][2] for m in members if scores[m][c.id][0]]
            blocked[c.name] = reasons
        near = min(blocked, key=lambda n: len(blocked[n]))
        cats = sorted({r for r in blocked[near]})
        yield contact, (f'No restaurant works for everyone at {when(slot)}. Closest: {near}, blocked by '
                        f"{len(blocked[near])} person(s) ({', '.join(cats)}). Deal breakers stay private and unchanged. "
                        'Try a different area or time.')

    def reservable(self, c):
        return False if c.name in self.walk_ins else getattr(c, 'reservable', None)

    # ---- action ----
    def book(self, member, contact):
        group, members = self.group_or_solo(member)
        p = self.plans.get(group or member)
        if not p:
            yield contact, 'No plan yet. Text PLAN dinner sat 7pm near Westwood first.'
            return
        city = p['area'].split(',')[-1].strip() or 'Los Angeles'
        options = p.get('options') or [(p['venue'], None)]
        top, top_res = options[0]
        # Google says walk-in only: nothing to reserve, so never spend a check on it.
        bookable = [name for name, res in options if res is not False][:BOOK_TRIES]
        if top_res is False:
            walk = f"{top} doesn't take reservations: just walk in, it's verified open {when(p['slot'])}."
            if not bookable:
                yield from self.to_all(members, walk, contact, member)
                return
            yield contact, f'{walk} Checking the next option that takes reservations, {bookable[0]}... (usually 1-3 minutes)'
        else:
            yield contact, f"Checking reservations for {p['size']} at {top}, {when(p['slot'])}... (usually 1-3 minutes)"
        misses = []
        for i, venue in enumerate(bookable):  # fair order among venues that take reservations
            b = self.check_table(venue, p['slot'], p['size'], city=city)
            if b.status == 'walk_in':
                self.walk_ins.add(venue)
            if b.status == 'ready' or i == len(bookable) - 1:
                break
            misses.append(venue)
            why = "doesn't take reservations (walk-in)" if b.status == 'walk_in' else 'has no table then'
            yield contact, f'{venue} {why}. Checking the runner-up, {bookable[i + 1]}...'
        note = f"Tried {', '.join(misses)} first. " if misses and b.status == 'ready' else ''
        if top_res is False:
            note = f'{top} is walk-in only. ' + note
        text = booking.message(b) + (f" It's verified open {when(p['slot'])}." if b.status == 'walk_in' else '')
        yield from self.to_all(members, note + text, contact, member)

    def busy(self, member, contact):
        group, members = self.group_or_solo(member)
        p = self.plans.get(group or member)
        if not p:
            yield contact, 'No plan to decline. Tell me which days you are busy, e.g. "busy saturday".'
            return
        day = p['slot'][:10]
        persona = self.persona(member)
        self.store.save_persona(member, validate_update(persona, {'busy_days': persona['hard'].get('busy_days', []) + [day]}))
        yield contact, f"Noted: you can't make {datetime.fromisoformat(day):%a %b} {int(day[8:])}. I told the group."
        yield from self.to_all([m for m in members if m != member],
                               f"{self.name(member)} can't make {when(p['slot'])}. Text PLAN with another day to re-plan.", contact, member)


def from_env():
    return Service(Store(os.environ['DATABASE_URL']))
