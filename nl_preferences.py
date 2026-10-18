"""Natural-language preference updates, translated by StepFun step-5-preview.

The model only turns one chat message into a JSON delta ("add vegetarian", "max $30").
It never sees the stored persona and never enforces anything:
- deltas are applied locally, then every field is re-checked by preferences.validate_update
- slots must be real event slots; unknown fields are rejected one by one
- the result is only a proposal; the owner must `confirm` it (same flow as `set ...`)
Explicit `set ...` commands skip the model, and model failures return a clear message.

Agent hook (one line in personal_agent.py): p = propose(text, persona); stage p.update; reply p.summary.
CLI: python nl_preferences.py maya "vegetarian now, can't do Friday, ideally under $20"
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Tuple

from negotiation import when
from preferences import parse_update, validate_update

STEPFUN_URL = 'https://api.stepfun.ai/v1/chat/completions'  # international platform; api.stepfun.com rejects these keys
STEPFUN_MODEL = 'step-5-preview'
DEMO_SLOTS = ('2026-10-23T19:00', '2026-10-24T19:00', '2026-10-25T18:00')  # organizer's candidate slots
VENUE_CUISINES = tuple(sorted({v['cuisine'] for v in json.loads((Path(__file__).resolve().parent / 'venues.json').read_text())}))

NUMBER_FIELDS = ('max_price', 'comfort_price', 'max_distance_min', 'comfort_distance_min')
LIST_FIELDS = ('dietary', 'never_cuisines', 'free_slots', 'preferred_slots', 'busy_days')
SLOT_FIELDS = ('free_slots', 'preferred_slots')
DAY_FIELDS = ('busy_days',)
WEIGHT_KEYS = ('cuisine', 'rating', 'distance', 'price')
KNOWN_KEYS = set(NUMBER_FIELDS) | {f'{f}_{op}' for f in LIST_FIELDS for op in ('add', 'remove')} | {'cuisine_pref', 'weights'}

SYSTEM = """You turn ONE chat message about group dinner preferences into a JSON object.
Use only these optional keys and omit anything the message does not state:
- max_price, comfort_price: USD per person. max = deal breaker ("can't go over", "max"); comfort = ideal ("ideally", "prefer under").
- max_distance_min, comfort_distance_min: travel minutes, same deal-breaker vs ideal split.
- dietary_add, dietary_remove: lists of food requirements, e.g. "vegetarian", "halal", "vegan".
- never_cuisines_add, never_cuisines_remove: cuisines they will never eat.
- cuisine_pref: object cuisine -> score 0..1 (1 love, 0.5 neutral, 0 dislike) for soft likes and dislikes.
- free_slots_add, free_slots_remove: KNOWN SLOTS they can / cannot attend.
- preferred_slots_add, preferred_slots_remove: KNOWN SLOTS they would rather have (soft).
- busy_days_add, busy_days_remove: KNOWN DAYS (YYYY-MM-DD) they cannot make at all / can make again.
- weights: object with any of cuisine, rating, distance, price -> importance 0..10.
Use lowercase names, the KNOWN CUISINES spelling when one matches, and exact KNOWN SLOTS strings.
Output {} if nothing applies. Output JSON only.
KNOWN CUISINES: """ + ', '.join(VENUE_CUISINES) + """
KNOWN SLOTS AND DAYS: """


@dataclass(frozen=True)
class Proposal:
    update: dict        # validate_update-ready; stage as pending, apply only on `confirm`
    summary: str        # owner-only wording of the change
    rejected: Tuple[str, ...]
    source: str         # 'stepfun' | 'command' | 'none'


class StepFunError(RuntimeError):
    pass


STEPFUN_HINTS = {401: 'check STEP_API_KEY', 402: 'check the StepFun balance or voucher',
                 403: 'check account permissions, balance and model access', 404: 'model not enabled for this account',
                 429: 'rate or quota limit, try again shortly'}
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1, 3)  # StepFun: "429 means retry with backoff". Timeouts are not retried (already 30 s).


def stepfun_chat(system, user, timeout=30, sleep=time.sleep):
    """THE Step 5 Preview call: one chat message in, a JSON delta out."""
    key = (os.environ.get('STEP_API_KEY') or os.environ.get('STEPFUN_API_KEY') or '').strip()
    if not key:
        raise StepFunError('STEP_API_KEY is not set')
    # Hidden reasoning counts against max_tokens; 800 left some answers empty. Low effort: ~3-9 s.
    body = {'model': STEPFUN_MODEL, 'temperature': 0, 'max_tokens': 4096, 'reasoning_effort': 'low',
            'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
    req = urllib.request.Request(STEPFUN_URL, data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    for delay in (*RETRY_DELAYS, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                choice = json.loads(r.read())['choices'][0]
            break
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUS and delay is not None:
                sleep(delay)
                continue
            raise StepFunError(f"StepFun HTTP {e.code}: {STEPFUN_HINTS.get(e.code, 'request failed')}") from None
    if choice.get('finish_reason') == 'length':
        raise StepFunError('StepFun answer was cut off at max_tokens')
    return choice['message'].get('content') or ''


def slots_line(slots, days=()):
    line = '; '.join(f'{s} = {datetime.fromisoformat(s):%A} {when(s)}' for s in slots)
    return line + ('\nDAYS: ' + '; '.join(f'{d} = {datetime.fromisoformat(d):%A %b} {int(d[8:])}' for d in days) if days else '')


def parse_json(text):
    """The model's JSON object, tolerating code fences or chatter around it."""
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('model returned no JSON object')
    raw = json.loads(text[start:end + 1])
    if not isinstance(raw, dict):
        raise ValueError('model returned non-object JSON')
    return raw


def number(v):
    if isinstance(v, bool):
        raise ValueError('not a number')
    v = float(v)
    return int(v) if v.is_integer() else v


def to_update(raw, persona, slots=DEMO_SLOTS, days=()):
    """Apply the model's delta to the persona's current values -> (update, rejected)."""
    update, rejected = {}, [f'{k}: unknown field' for k in raw if k not in KNOWN_KEYS]
    known = set(VENUE_CUISINES) | set(persona['cuisine_pref'])
    cuisine = lambda c: c[:-1] if c not in known and c[:-1] in known and c.endswith('s') else c  # "burgers" -> "burger"
    for k in NUMBER_FIELDS:
        if raw.get(k) is not None:
            try:
                update[k] = number(raw[k])
            except (TypeError, ValueError):
                rejected.append(f'{k}: not a number')
    for f in LIST_FIELDS:
        add, rem = raw.get(f + '_add') or [], raw.get(f + '_remove') or []
        if not isinstance(add, list) or not isinstance(rem, list) or not all(isinstance(x, str) for x in add + rem):
            rejected.append(f'{f}: expected lists of text')
            continue
        if f in SLOT_FIELDS or f in DAY_FIELDS:
            norm = str.strip
        elif f == 'never_cuisines':
            norm = lambda x: cuisine(x.strip().lower())
        else:
            norm = lambda x: x.strip().lower()
        add, rem = [norm(x) for x in add], [norm(x) for x in rem]
        if f in SLOT_FIELDS or f in DAY_FIELDS:
            allowed = slots if f in SLOT_FIELDS else days
            rejected += [f'{f}: unknown {"slot" if f in SLOT_FIELDS else "day"} {x}' for x in add + rem if x not in allowed]
            add, rem = [x for x in add if x in allowed], [x for x in rem if x in allowed]
        if add or rem:
            current = (persona.get('soft', {}) if f == 'preferred_slots' else persona['hard']).get(f, [])
            update[f] = [x for x in current if x not in rem] + [x for x in add if x not in current]
    if isinstance(raw.get('cuisine_pref'), dict) and raw['cuisine_pref']:
        update['cuisine_pref'] = {**persona['cuisine_pref'], **{cuisine(str(k).strip().lower()): v for k, v in raw['cuisine_pref'].items()}}
    if isinstance(raw.get('weights'), dict) and raw['weights']:
        rejected += [f'weights: unknown key {k}' for k in raw['weights'] if k not in WEIGHT_KEYS]
        old = persona['weights']  # stored weights use any scale; bring them to the model's 0..10 first
        scale = 10 / max(old.values()) if max(old.values()) > 0 else 1
        update['weights'] = {**{k: round(v * scale, 2) for k, v in old.items()},
                             **{k: v for k, v in raw['weights'].items() if k in WEIGHT_KEYS}}
    valid = {}
    for k, v in update.items():
        try:
            if validate_update(persona, {k: v}) != persona:  # drop no-ops
                valid[k] = v
        except (ValueError, TypeError) as e:
            rejected.append(f'{k}: {e}')
    return valid, tuple(rejected)


def current(persona, k):
    if k in ('comfort_price', 'comfort_distance_min', 'preferred_slots'):
        return persona.get('soft', {}).get(k)
    return persona[k] if k in ('weights', 'cuisine_pref') else persona['hard'].get(k)


def summary(persona, update):
    lines = []
    for k, new in update.items():
        old = current(persona, k)
        if k in LIST_FIELDS:
            day = lambda d: f'{datetime.fromisoformat(d):%a %b} {int(d[8:])}'
            old, fmt = old or [], (when if k in SLOT_FIELDS else day if k in DAY_FIELDS else str)
            lines += [f'{k}: + {fmt(x)}' for x in new if x not in old] + [f'{k}: - {fmt(x)}' for x in old if x not in new]
        elif k == 'weights':  # scales differ, so compare each factor's share of the decision
            share = lambda w, c: w.get(c, 0) / sum(w.values())
            lines += [f'weights.{c}: {share(old, c):.0%} -> {share(new, c):.0%} of the decision'
                      for c in new if round(share(old, c), 2) != round(share(new, c), 2)]
        elif isinstance(new, dict):
            lines += [f'{k}.{c}: {old.get(c, "unset")} -> {v}' for c, v in new.items() if old.get(c) != v]
        else:
            lines.append(f'{k}: {"unset" if old is None else old} -> {new}')
    return '\n'.join(lines) or 'No preference changes found.'


def propose(text, persona, llm: Callable[[str, str], str] = stepfun_chat, slots=DEMO_SLOTS, days=()) -> Proposal:
    if re.match(r'\s*set\s', text, re.I):
        try:
            update = parse_update(text)
            validate_update(persona, update)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return Proposal({}, '', (str(e),), 'command')
        return Proposal(update, summary(persona, update), (), 'command')
    try:
        raw = parse_json(llm(SYSTEM + slots_line(slots, days), text))
    except Exception as e:  # network, auth, timeout, bad JSON: never crash the agent
        reason = str(e) if isinstance(e, StepFunError) else type(e).__name__
        return Proposal({}, '', (f'language model unavailable ({reason}); try: set max_price 30',), 'none')
    update, rejected = to_update(raw, persona, slots, days)
    return Proposal(update, summary(persona, update), rejected, 'stepfun')


if __name__ == '__main__':
    from pathlib import Path
    from preferences import load_persona
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python nl_preferences.py maya|jordan|sam "message"')
    p = propose(sys.argv[2], load_persona(sys.argv[1], Path(__file__).resolve().parent))
    print(f'[{p.source}] proposed update: {json.dumps(p.update)}\n{p.summary}')
    if p.rejected:
        print('rejected:', *p.rejected, sep='\n  ')
