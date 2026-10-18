import copy
import json
import random
import re
import unittest
from datetime import datetime
from types import SimpleNamespace

import booking
from consensus_service import DEFAULT_AREA, Service, parse_area, parse_when

NOW = datetime(2026, 10, 18, 1, 0)   # Sunday 1am
SAT = '2026-10-24T19:00'


class FakeStore:
    def __init__(self):
        self.members, self.personas, self.contacts, self.groups, self.plans, self.credits = {}, {}, {}, {}, [], {}

    def add_member(self, m, name): self.members[m] = name
    def member_name(self, m): return self.members.get(m)
    def get_persona(self, m): return copy.deepcopy(self.personas.get(m))
    def save_persona(self, m, p): self.personas[m] = copy.deepcopy(p)
    def save_contact(self, m, ch, space, agent): self.contacts[m] = {'channel': ch, 'space_id': space, 'agent_id': agent}
    def get_contact(self, m): return self.contacts.get(m)
    def group_exists(self, g): return g in self.groups
    def create_group(self, g, name, ms): self.groups.setdefault(g, []).extend(x for x in ms if x not in self.groups[g])
    def group_members(self, g): return sorted(self.groups[g])
    def member_group(self, m): return next((g for g in reversed(list(self.groups)) if m in self.groups[g]), None)
    def get_credits(self, g): return self.credits.get(g, {})
    def record_plan(self, pid, g, d): self.plans.append((pid, g, d.chosen))


def venue(vid, price=25, tags=('vegetarian',), rating=4.5, cuisine='thai', reservable=None):
    return SimpleNamespace(id=f'{vid}@{SAT}', name=vid.title(), cuisine=cuisine, price=price, rating=rating,
                           distance_min=10, tags=list(tags), slot=SAT, reservable=reservable)


class Harness:
    def __init__(self, llm_reply=None, places=None, table=None):
        self.store = FakeStore()
        self.calls = []
        self.svc = Service(self.store, llm=lambda s, u: json.dumps(llm_reply or {}),
                           places=places or (lambda area, slot: ([venue('greens'), venue('steakhouse', tags=(), rating=4.9, cuisine='steak')], [('Gone', 'closed_permanently')])),
                           check_table=table or (lambda v, s, n, city: booking.Booking('ready', v, s, n, 'https://www.opentable.com/x', ('7:00 PM',))),
                           now=lambda: NOW, rng=random.Random(1))

    def say(self, member, text):
        contact = {'channel': 'imessage', 'space_id': f'space-{member}', 'agent_id': 'agent'}
        out = list(self.svc.handle(member, text, contact))
        return [(c['space_id'].removeprefix('space-'), t) for c, t in out]


class ServiceTests(unittest.TestCase):
    def test_new_user_gets_welcome_and_private_defaults(self):
        h = Harness()
        out = h.say('+1A', 'hi')
        self.assertEqual(len(out), 1)
        self.assertIn('Welcome to ConsensusAI', out[0][1])
        self.assertIn('max_price', json.dumps(h.store.personas['+1A']))
        self.assertEqual(h.store.contacts['+1A']['space_id'], 'space-+1A')

    def test_preferences_need_yes(self):
        h = Harness(llm_reply={'dietary_add': ['vegetarian'], 'max_price': 40})
        h.say('+1A', 'hi')
        out = h.say('+1A', "I'm vegetarian and can spend up to $40")
        self.assertIn('Reply YES to save', out[0][1])
        self.assertEqual(h.store.personas['+1A']['hard']['dietary'], [])   # nothing saved yet
        self.assertIn('Saved', h.say('+1A', 'yes')[0][1])
        self.assertEqual(h.store.personas['+1A']['hard']['dietary'], ['vegetarian'])
        self.assertIn('budget up to $40', h.say('+1A', 'show')[0][1])
        self.assertIn('Nothing pending', h.say('+1A', 'yes')[0][1])

    def test_group_flow_plan_respects_every_deal_breaker(self):
        h = Harness()
        h.say('+1A', 'hi'); h.say('+1B', 'hi')
        code = re.search(r'JOIN (\w+)', h.say('+1A', 'start group')[0][1]).group(1)
        out = h.say('+1B', f'join {code.lower()}')
        self.assertEqual([to for to, _ in out], ['+1B', '+1A'])           # A is told B joined
        h.store.personas['+1A']['hard']['dietary'] = ['vegetarian']      # steakhouse is not tagged vegetarian
        out = h.say('+1B', 'plan dinner saturday 7pm near Westwood')
        self.assertEqual(sorted(to for to, _ in out), ['+1A', '+1B'])     # everyone gets the plan
        for _, text in out:
            self.assertIn('Greens', text)                                 # higher-rated steakhouse is vetoed for A
            self.assertNotIn('Steakhouse (', text)
            self.assertIn('Verified open on Google', text)
            self.assertIn('Your match:', text)
        self.assertEqual(h.store.plans[0][1], code)

    def test_busy_then_replan_tells_organizer(self):
        h = Harness()
        h.say('+1A', 'hi'); h.say('+1B', 'hi')
        code = re.search(r'JOIN (\w+)', h.say('+1A', 'start group')[0][1]).group(1)
        h.say('+1B', f'join {code}')
        h.say('+1A', 'plan dinner sat 7pm')
        out = h.say('+1B', 'busy')
        self.assertEqual(h.store.personas['+1B']['hard']['busy_days'], ['2026-10-24'])
        self.assertIn("can't make", dict(out)['+1A'])
        h.say('+1B', 'name Alex')
        out = h.say('+1A', 'plan dinner saturday 7pm')
        self.assertEqual(out, [('+1A', "Alex can't make Oct 24 at 7pm. Try another day, e.g. PLAN dinner sunday 7pm.")])

    def test_book_says_checking_first_then_tells_everyone(self):
        seen = []
        h = Harness(table=lambda v, s, n, city: seen.append((v, s, n, city)) or booking.Booking('ready', v, s, n, 'https://www.opentable.com/x', ('7:00 PM',)))
        h.say('+1A', 'hi')
        self.assertIn('No plan yet', h.say('+1A', 'book')[0][1])
        h.say('+1A', 'plan dinner sat 7pm near Westwood')
        out = h.say('+1A', 'book')
        self.assertIn('Checking OpenTable', out[0][1])
        self.assertIn('Not booked yet', out[1][1])
        self.assertEqual(seen, [('Steakhouse', SAT, 1, 'Los Angeles')])

    def test_book_falls_back_to_runner_up(self):
        tried = []

        def table(v, s, n, city):
            tried.append(v)
            status = 'unavailable' if v == 'Steakhouse' else 'ready'
            return booking.Booking(status, v, s, n, 'https://www.opentable.com/x', ('7:15 PM',))
        h = Harness(table=table)
        h.say('+1A', 'hi')
        h.say('+1A', 'plan dinner sat 7pm')
        out = h.say('+1A', 'book')
        self.assertEqual(tried, ['Steakhouse', 'Greens'])   # fair order: top pick, then runner-up
        self.assertIn('usually 1-3 minutes', out[0][1])
        self.assertIn('Checking the runner-up, Greens', out[1][1])
        self.assertTrue(out[2][1].startswith('No table at Steakhouse. A table for 1 at Greens'))

    def test_book_stops_after_two_tries(self):
        tried = []
        h = Harness(table=lambda v, s, n, city: tried.append(v) or booking.Booking('unavailable', v, s, n, 'https://x', ()),
                    places=lambda a, s: ([venue('a'), venue('b'), venue('c')], []))
        h.say('+1A', 'hi')
        h.say('+1A', 'plan dinner sat 7pm')
        out = h.say('+1A', 'book')
        self.assertEqual(len(tried), 2)
        self.assertIn('has no table', out[-1][1])

    def test_walk_in_top_pick_is_never_checked(self):
        tried = []
        places = lambda a, s: ([venue('walkin', rating=4.9, reservable=False), venue('bistro', reservable=True),
                                venue('cafe', reservable=False)], [])
        h = Harness(places=places, table=lambda v, s, n, city: tried.append(v) or booking.Booking('ready', v, s, n, 'https://x', ()))
        h.say('+1A', 'hi')
        self.assertIn('walk-in only', h.say('+1A', 'plan dinner sat 7pm')[0][1])
        out = h.say('+1A', 'book')
        self.assertEqual(tried, ['Bistro'])
        self.assertIn("Walkin doesn't take reservations", out[0][1])
        self.assertTrue(out[-1][1].startswith('Walkin is walk-in only. A table for 1 at Bistro'))

    def test_all_walk_in_means_no_checks(self):
        tried = []
        h = Harness(places=lambda a, s: ([venue('walkin', reservable=False)], []),
                    table=lambda v, s, n, city: tried.append(v))
        h.say('+1A', 'hi')
        h.say('+1A', 'plan dinner sat 7pm')
        out = h.say('+1A', 'book')
        self.assertEqual(tried, [])
        self.assertEqual(len(out), 1)
        self.assertIn('just walk in', out[0][1])

    def test_places_failure_never_invents_a_venue(self):
        def down(area, slot):
            raise OSError('network')
        h = Harness(places=down)
        h.say('+1A', 'hi')
        out = h.say('+1A', 'plan dinner sat 7pm')
        self.assertEqual(len(out), 1)
        self.assertIn("couldn't search restaurants", out[0][1])

    def test_no_plan_reports_categories_not_limits(self):
        h = Harness(places=lambda a, s: ([venue('pricey', price=90)], []))
        h.say('+1A', 'hi')
        h.store.personas['+1A']['hard']['max_price'] = 37
        out = h.say('+1A', 'plan dinner sat 7pm')
        self.assertIn('over budget', out[0][1])
        self.assertNotIn('37', out[0][1])

    def test_unknown_group_code(self):
        h = Harness()
        self.assertIn("can't find group", h.say('+1A', 'join ZZZZZ')[0][1])


class ParseTests(unittest.TestCase):
    def test_when(self):
        self.assertEqual(parse_when('plan dinner saturday 7pm', NOW), SAT)
        self.assertEqual(parse_when('plan sat 7:30pm', NOW), '2026-10-24T19:30')
        self.assertEqual(parse_when('plan dinner', NOW), SAT)                       # default Saturday 7pm
        self.assertEqual(parse_when('plan tomorrow 8pm', NOW), '2026-10-19T20:00')
        self.assertEqual(parse_when('plan sunday 18:00', NOW), '2026-10-18T18:00')  # later today
        self.assertEqual(parse_when('plan sunday 12am', NOW), '2026-10-25T00:00')   # already passed -> next week

    def test_area(self):
        self.assertEqual(parse_area('plan dinner sat 7pm near Westwood'), 'Westwood, Los Angeles')
        self.assertEqual(parse_area('plan dinner in Santa Monica'), 'Santa Monica, Los Angeles')
        self.assertEqual(parse_area('plan dinner sat 7pm'), DEFAULT_AREA)
        self.assertEqual(parse_area('plan near Pasadena, CA'), 'Pasadena, CA')


if __name__ == '__main__':
    unittest.main()
