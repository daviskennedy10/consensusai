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
import constraints
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
IN_WORDS = ('in', "i'm in", 'im in', 'count me in', "i'm down", 'im down')
BUSY_WORDS = ('busy', "can't make it", 'cant make it', "can't", 'cant')
OUT_WORDS = ('out', "i'm out", 'im out', 'pass') + BUSY_WORDS
HELP = ("ConsensusAI plans group outings fairly, and keeps your deal breakers private.\n"
        "- Tell me what you like: \"I'm vegetarian, max $30, I love thai\"\n"
        "- START GROUP, then friends text JOIN <code>\n"
        "- PLAN dinner sat 7pm near Westwood with Eddie and Mike (their agents ask them)\n"
        "- PLAN movie: watch Primetime sat 8pm near Westwood\n"
        "- IN / OUT to answer an invite, GO to plan with whoever is in\n"
        "- BOOK to check a table or showtimes, BUSY if you can't make it\n"
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


def parse_with(text):
    """Names after 'with' ("with Eddie and Mike"), and the text without that clause."""
    m = re.search(r'\bwith\s+(.+?)(?=\s+(?:near|in|around|at|on|this|next)\s|$)', text, re.I)
    if not m:
        return [], text
    names = [n.strip(' .!?') for n in re.split(r',|\band\b|&', m.group(1)) if n.strip(' .!?')]
    return names, (text[:m.start()] + text[m.end():]).strip()


def parse_activity(text):
    """(Places kind, label): movies search theaters; everything else is food."""
    t = text.lower()
    if re.search(r'\b(movie|movies|film|cinema|watch)\b', t):
        m = re.search(r'\bwatch\s+(.+?)(?=\s+(?:at|on|next|this|tonight|today|tomorrow|near|in|around|'
                      + '|'.join(d[:3] + '(?:' + d[3:] + ')?' for d in DAYS) + r')\b|\s+\d|$)', text, re.I)
        title = m.group(1).strip() if m else ''
        return 'movie theaters', (f'to watch {title}' if title and title.lower() not in ('a movie', 'movie', 'a film') else 'a movie')
    meal = next((w for w in ('brunch', 'lunch', 'breakfast', 'drinks', 'coffee') if re.search(rf'\b{w}\b', t)), 'dinner')
    return 'restaurants', meal


def parse_area(text):
    m = re.search(r'\b(?:near|in|around)\s+([a-z0-9 .,\'-]+?)\s*[.!?]*$', text, re.I)
    area = m.group(1).strip() if m else ''
    if not area or re.fullmatch(r'(the )?(area|town|city)', area, re.I):
        return DEFAULT_AREA
    return area if ',' in area or 'los angeles' in area.lower() else f'{area}, Los Angeles'


class PersonalAgent:
    """One person's agent. It holds their private persona and answers other agents only with
    can/can't, scores and veto categories, never the persona itself."""

    def __init__(self, member, persona):
        self.member, self._persona = member, persona

    def busy_at(self, slot):
        h = self._persona['hard']
        return slot[:10] in h.get('busy_days', []) or ('free_slots' in h and slot not in h['free_slots'])

    def score(self, candidates):
        out = {}
        for c in candidates:
            veto, score, reason = evaluate(self._persona, c)
            out[c.id] = (veto, score, constraints.category_for_reason(reason) if veto else '')
        return out


class Service:
    def __init__(self, store, llm=nl_preferences.stepfun_chat, places=venues.search,
                 check_table=booking.check_availability, now=datetime.now, rng=random.Random()):
        self.store, self.llm, self.places, self.check_table, self.now, self.rng = store, llm, places, check_table, now, rng
        self.pending = {}   # member -> proposed update. shortcut: in-process; a restart drops unconfirmed proposals.
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
        elif cmd in IN_WORDS:
            yield from self.rsvp(member, True, contact)
        elif cmd in OUT_WORDS:
            yield from self.rsvp(member, False, contact, busy=cmd in BUSY_WORDS)
        elif cmd == 'go':
            yield from self.go(member, contact)
        elif cmd in ('book', 'book it', 'reserve'):
            yield from self.book(member, contact)
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
        named = self.store.member_name(member) and not self.store.member_name(member).startswith('Friend ')
        yield contact, (f'You joined group {code} ({len(members)} people). ' + ('' if named else
                        'Text NAME <your name> so friends can invite you. ') + 'Tell me your preferences anytime.')
        yield from self.to_all([m for m in members if m != member], f'{self.name(member)} joined group {code}.', contact, member)

    # ---- planning: the organizer's agent invites the others' agents ----
    def agent(self, member):
        return PersonalAgent(member, self.persona(member))

    def contact_of(self, member):
        return self.store.get_contact(member)

    def plan(self, member, text, contact):
        group, members = self.group_or_solo(member)
        names, rest = parse_with(text)
        kind, label = parse_activity(rest)
        slot, area = parse_when(rest, self.now()), parse_area(rest)
        if names:
            if not group:
                yield contact, 'Start a group first: text START GROUP, then your friends text JOIN <code> and NAME <name>.'
                return
            by_name = {self.name(m).lower(): m for m in members if m != member}
            invitees = [by_name[n.lower()] for n in names if n.lower() in by_name]
            unknown = [n for n in names if n.lower() not in by_name]
            if unknown:
                yield contact, (f"{' and '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not in group {group} yet. "
                                f'Ask them to text this number JOIN {group}, then NAME <their name>.')
                if not invitees:
                    return
        else:
            invitees = [m for m in members if m != member]
        event = label.removeprefix('to watch ') if label.startswith('to watch ') else label  # 'Primetime' / 'dinner'
        s = {'key': group or member, 'group': group, 'organizer': member, 'kind': kind, 'label': label, 'event': event,
             'slot': slot, 'area': area, 'answers': {member: 'in'}, 'status': 'inviting'}
        where, out_msgs, invites = area.split(',')[0], [], []
        for m in invitees:
            reach = self.contact_of(m)
            if self.agent(m).busy_at(slot):  # the friend's agent answers from private data; no reason shared
                s['answers'][m] = 'out'
                out_msgs.append(f"{self.name(m)}'s agent says {self.name(m)} can't make {when(slot)}.")
            elif not reach:
                s['answers'][m] = 'out'
                out_msgs.append(f"I can't reach {self.name(m)}'s agent right now.")
            else:
                s['answers'][m] = 'asked'
                invites.append((reach, f'{self.name(member)} wants to plan {label} {when(slot)} near {where}. '
                                       'Are you in? Reply IN or OUT.'))
        self.store.save_session(s['key'], s)  # saved before anyone is messaged
        for msg in out_msgs:
            yield contact, msg
        yield from invites
        asked = [m for m, a in s['answers'].items() if a == 'asked']
        if asked:
            yield contact, (f"I asked {' and '.join(self.name(m) for m in asked)}'s agents. I'll plan as soon as they answer, "
                            'or text GO to plan with whoever is in.')
        else:
            yield from self.finalize(s)

    def rsvp(self, member, yes, contact, busy=False):
        s = self.store.invited_session(member)
        if not s:
            if busy:
                yield from self.busy_after_plan(member, contact)
            else:
                yield contact, 'No open invitation right now.'
            return
        s['answers'][member] = 'in' if yes else 'out'
        if busy:
            self.add_busy_day(member, s['slot'])
        self.store.save_session(s['key'], s)
        org = s['organizer']
        yield contact, ("You're in. I'll send the plan once everyone answers." if yes
                        else f"Got it, you're out. I let {self.name(org)} know.")
        oc = self.contact_of(org)
        if oc:
            yield oc, f"{self.name(member)} is {'in' if yes else 'out'} for {s.get('event', s['label'])} {when(s['slot'])}."
        if not any(a == 'asked' for a in s['answers'].values()):
            yield from self.finalize(s)

    def go(self, member, contact):
        group, _ = self.group_or_solo(member)
        s = self.store.get_session(group or member)
        if not s or s['status'] != 'inviting' or s['organizer'] != member:
            yield contact, 'Nothing is waiting on answers. Text PLAN to start.'
            return
        yield from self.finalize(s)

    def finalize(self, s):
        attendees = [m for m, a in s['answers'].items() if a == 'in']
        skipped = [m for m, a in s['answers'].items() if a != 'in']
        org, oc = s['organizer'], self.contact_of(s['organizer'])
        for m, a in s['answers'].items():
            if a == 'asked':
                s['answers'][m] = 'missed'
        try:
            candidates, dropped = self.places(s['area'], s['slot'], kind=s['kind'])
        except Exception as e:  # Places down: say so, never invent venues
            s['status'] = 'failed'
            self.store.save_session(s['key'], s)
            if oc:
                yield oc, f"I couldn't search {s['kind']} right now ({type(e).__name__}). Text PLAN again in a minute."
            return
        place = 'theaters' if s['kind'] != 'restaurants' else 'restaurants'
        if not candidates:
            s['status'] = 'none'
            self.store.save_session(s['key'], s)
            if oc:
                yield oc, f"No {place} near {s['area']} are verified open at {when(s['slot'])}. Try another time or area."
            return
        scores = {m: self.agent(m).score(candidates) for m in attendees}  # each agent scores privately
        table = {c.id: {m: scores[m][c.id][1] for m in attendees} for c in candidates
                 if not any(scores[m][c.id][0] for m in attendees)}
        if not table:
            s['status'] = 'none'
            self.store.save_session(s['key'], s)
            if oc:
                yield oc, self.no_plan(attendees, scores, candidates, s['slot'], place)
            return
        by_id = {c.id: c for c in candidates}
        group = s['group']
        d = fairness.decide(table, history=self.store.get_credits(group) if group else None,
                            names={cid: by_id[cid].name for cid in table})
        c = by_id[d.chosen]
        if group:
            self.store.record_plan(f'{group}-{self.rng.randrange(16**6):06x}', group, d)
        s['status'] = 'planned'
        s['plan'] = {'venue': c.name, 'size': len(attendees), 'attendees': attendees, 'maps_url': getattr(c, 'maps_url', ''),
                     'options': [(by_id[cid].name, self.reservable(by_id[cid])) for cid in d.ranking[:OPTIONS_KEPT]]}
        self.store.save_session(s['key'], s)
        who = f'the {len(attendees)} of you' if len(attendees) > 1 else 'you'
        res = {True: ', takes reservations', False: ', walk-in only'}.get(self.reservable(c), '') if s['kind'] == 'restaurants' else ''
        unit = 'per ticket, estimated' if s['kind'] != 'restaurants' else 'per person'
        base = (f"Plan for {who} ({s['label']}): {c.name} ({c.cuisine}, {c.rating:.1f} stars, about ${c.price} {unit}{res}), "
                f"{when(s['slot'])} near {s['area'].split(',')[0]}.\n{d.explanation}\n"
                f'Verified open on Google just now ({len(dropped)} closed or unverified places skipped).')
        action = 'check showtimes' if s['kind'] != 'restaurants' else 'check a real table'
        tail = f'\nReply BOOK to {action}' + (', or BUSY if you cannot make it.' if group else '.')
        for m in attendees:
            target = self.contact_of(m)
            if target:
                yield target, f'{base}\nYour match: {table[d.chosen][m]:.0%}.{tail}'
        if skipped and oc:
            yield oc, f"Not included: {', '.join(self.name(m) for m in skipped)}."

    def no_plan(self, attendees, scores, candidates, slot, place):
        busy = [m for m in attendees if any(v and cat == 'availability' for v, _, cat in scores[m].values())]
        if busy:
            return f"{', '.join(self.name(m) for m in busy)} can't make {when(slot)}. Try another day, e.g. PLAN dinner sunday 7pm."
        blocked = {c.name: [scores[m][c.id][2] for m in attendees if scores[m][c.id][0]] for c in candidates}
        near = min(blocked, key=lambda n: len(blocked[n]))
        return (f"No {place[:-1]} works for everyone at {when(slot)}. Closest: {near}, blocked by {len(blocked[near])} "
                f"person(s) ({', '.join(sorted(set(blocked[near])))}). Deal breakers stay private and unchanged. "
                'Try a different area or time.')

    def reservable(self, c):
        return False if c.name in self.walk_ins else getattr(c, 'reservable', None)

    def planned(self, member):
        group, _ = self.group_or_solo(member)
        s = self.store.get_session(group or member)
        return s if s and s.get('status') == 'planned' else None

    # ---- action ----
    def book(self, member, contact):
        s = self.planned(member)
        if not s:
            yield contact, 'No plan yet. Text PLAN dinner sat 7pm near Westwood first.'
            return
        p, slot, attendees = s['plan'], s['slot'], s['plan']['attendees']
        city = s['area'].split(',')[-1].strip() or 'Los Angeles'
        if s['kind'] != 'restaurants':
            yield contact, f"Checking showtimes at {p['venue']} near {when(slot)}... (usually 1-3 minutes)"
            b = self.check_table(p['venue'], slot, p['size'], city=city, what=s['label'])
            yield from self.to_all(attendees, booking.message(b), contact, member)
            return
        options = p.get('options') or [(p['venue'], None)]
        top, top_res = options[0]
        # Google says walk-in only: nothing to reserve, so never spend a check on it.
        bookable = [name for name, res in options if res is not False][:BOOK_TRIES]
        if top_res is False:
            walk = f"{top} doesn't take reservations: just walk in, it's verified open {when(slot)}."
            if not bookable:
                yield from self.to_all(attendees, walk, contact, member)
                return
            yield contact, f'{walk} Checking the next option that takes reservations, {bookable[0]}... (usually 1-3 minutes)'
        else:
            yield contact, f"Checking reservations for {p['size']} at {top}, {when(slot)}... (usually 1-3 minutes)"
        misses = []
        for i, venue in enumerate(bookable):  # fair order among venues that take reservations
            b = self.check_table(venue, slot, p['size'], city=city)
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
        text = booking.message(b) + (f" It's verified open {when(slot)}." if b.status == 'walk_in' else '')
        yield from self.to_all(attendees, note + text, contact, member)

    def add_busy_day(self, member, slot):
        persona = self.persona(member)
        days = persona['hard'].get('busy_days', [])
        if slot[:10] not in days:
            self.store.save_persona(member, validate_update(persona, {'busy_days': days + [slot[:10]]}))

    def busy_after_plan(self, member, contact):
        s = self.planned(member)
        if not s or member not in s['plan']['attendees']:
            yield contact, 'No plan to decline. Tell me which days you are busy, e.g. "busy saturday".'
            return
        day = s['slot'][:10]
        self.add_busy_day(member, s['slot'])
        yield contact, f"Noted: you can't make {datetime.fromisoformat(day):%a %b} {int(day[8:])}. I told the group."
        yield from self.to_all([m for m in s['plan']['attendees'] if m != member],
                               f"{self.name(member)} can't make {when(s['slot'])}. Text PLAN with another day to re-plan.",
                               contact, member)


def from_env():
    return Service(Store(os.environ['DATABASE_URL']))
