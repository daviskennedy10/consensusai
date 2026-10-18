"""Table booking via Browser Use cloud, run after the group approves a plan.

Dry run (the only mode for now): a cloud browser agent opens the venue's booking page and
checks real availability for the party, then STOPS before entering personal details or
clicking any final reserve button. It never books.
shortcut: no live booking yet; it needs real group approval plus the booker's contact
details passed as Browser Use `secrets`. Add it once the approval flow exists.

Never raises: a missing key, network error, blocked site, failed task or timeout returns
status 'fallback' with an OpenTable deep link the group can use by hand.
CLI: python booking.py "Water Grill" 2026-10-24T19:00 3
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

API = 'https://api.browser-use.com/api/v2'
MAX_STEPS = 20          # bounds cost; the v2 API has no stop-task call
TIMEOUT_S = 180         # live checks took 21 s to 3 min
POLL_S = 3
DONE = {'finished', 'failed', 'stopped'}
RESULT_SCHEMA = json.dumps({
    'type': 'object',
    'properties': {
        'outcome': {'type': 'string', 'enum': ['available', 'unavailable', 'no_reservations', 'not_found', 'blocked']},
        'bookable_times': {'type': 'array', 'items': {'type': 'string'}},
        'booking_url': {'type': 'string'},
        'notes': {'type': 'string'},
    },
    'required': ['outcome'],
})


@dataclass(frozen=True)
class Booking:
    status: str                      # 'ready' (slot bookable, not booked) | 'unavailable' | 'walk_in' | 'fallback'
    venue: str
    slot: str
    party_size: int
    link: str                        # where a human finishes the booking
    times: Tuple[str, ...] = ()      # bookable times the agent saw near the slot
    notes: str = ''
    task_id: Optional[str] = None
    cost: Optional[str] = None


def deep_link(venue, slot, party_size, city='Los Angeles'):
    q = urllib.parse.urlencode({'dateTime': slot, 'covers': party_size, 'term': f'{venue} {city}'})
    return f'https://www.opentable.com/s?{q}'


def dry_run_task(venue, slot, party_size, city):
    date, hhmm = slot.split('T')
    return (f'Find the restaurant "{venue}" in {city} on OpenTable (or its own reservation page). '
            f'Check whether a table for {party_size} is available on {date} at {hhmm}, and note bookable times '
            'within 90 minutes of it. If the restaurant takes no online reservations at all (walk-in only), '
            'report outcome no_reservations. This is an availability check only: NEVER enter a name, phone, email or '
            'card, and NEVER click a final reserve, complete or confirm button. Stop on the page where a human '
            'would enter their details, and report the outcome, the bookable times, and that page URL.')


def http(method, path, body=None, timeout=30):
    key = os.environ.get('BROWSER_USE_API_KEY')
    if not key:
        raise RuntimeError('BROWSER_USE_API_KEY is not set')
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-Browser-Use-API-Key': key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def check_availability(venue, slot, party_size, city='Los Angeles', request=http, sleep=time.sleep,
                       timeout_s=TIMEOUT_S) -> Booking:
    if not 1 <= int(party_size) <= 20:
        raise ValueError('party_size must be between 1 and 20')
    link = deep_link(venue, slot, party_size, city)
    fallback = lambda notes, task_id=None, cost=None: Booking('fallback', venue, slot, party_size, link, (), notes, task_id, cost)
    try:
        task = request('POST', '/tasks', {'task': dry_run_task(venue, slot, party_size, city), 'startUrl': link,
                                          'maxSteps': MAX_STEPS, 'structuredOutput': RESULT_SCHEMA,
                                          'metadata': {'app': 'consensusai', 'mode': 'dry_run'}})
        task_id, waited = task['id'], 0
        while True:
            status = request('GET', f'/tasks/{task_id}/status')
            if status.get('status') in DONE:
                break
            if waited >= timeout_s:
                return fallback('booking agent timed out; finish with the link', task_id)
            sleep(POLL_S)
            waited += POLL_S
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, KeyError, ValueError) as e:
        return fallback(f'booking agent unavailable ({type(e).__name__}); finish with the link')
    cost = status.get('cost')
    if status['status'] != 'finished':
        return fallback(f"booking agent {status['status']}; finish with the link", task_id, cost)
    try:
        out = json.loads(status.get('output') or '')
        outcome = out['outcome']
    except (ValueError, KeyError, TypeError):
        return fallback('booking agent gave no readable result; finish with the link', task_id, cost)
    page = out.get('booking_url') or ''
    page = page if page.startswith('https://') else link  # only hand people an https page the agent reached
    times = tuple(str(t) for t in out.get('bookable_times') or ())
    if outcome == 'available':
        return Booking('ready', venue, slot, party_size, page, times, out.get('notes', ''), task_id, cost)
    if outcome == 'unavailable':
        return Booking('unavailable', venue, slot, party_size, page, times, out.get('notes', ''), task_id, cost)
    if outcome == 'no_reservations':  # not full: there is simply nothing to book
        return Booking('walk_in', venue, slot, party_size, page, (), out.get('notes', ''), task_id, cost)
    return fallback(f"booking site {outcome}: {out.get('notes', '')}".strip(), task_id, cost)


def message(b: Booking) -> str:
    """Chat-ready line for the organizer."""
    when = b.slot.replace('T', ' at ')
    if b.status == 'ready':
        near = f" Other times: {', '.join(b.times)}." if b.times else ''
        return f'A table for {b.party_size} at {b.venue} on {when} is available.{near} Not booked yet; confirm here: {b.link}'
    if b.status == 'walk_in':
        return f"{b.venue} doesn't take reservations: just walk in."
    if b.status == 'unavailable':
        near = f" Nearby times: {', '.join(b.times)}." if b.times else ' No nearby times.'
        return f'{b.venue} has no table for {b.party_size} on {when}.{near} {b.link}'
    return f'Could not check {b.venue} automatically ({b.notes}): {b.link}'


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('Usage: python booking.py "Venue name" 2026-10-24T19:00 3')
    b = check_availability(sys.argv[1], sys.argv[2], int(sys.argv[3]))
    print(message(b))
    print(f'[status={b.status} task={b.task_id} cost=${b.cost}]')
