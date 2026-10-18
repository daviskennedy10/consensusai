import copy
import json
import random
import re
import unittest
from datetime import datetime
from types import SimpleNamespace

import booking
from consensus_service import DEFAULT_AREA, Service, parse_activity, parse_area, parse_when, parse_with

NOW = datetime(2026, 10, 18, 1, 0)   # Sunday 1am
SAT = '2026-10-24T19:00'


class FakeStore:
    def __init__(self):
        self.members, self.personas, self.contacts, self.groups, self.plans, self.credits = {}, {}, {}, {}, [], {}
        self.sessions = {}

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
    def save_session(self, key, s): self.sessions[key] = copy.deepcopy(s)
    def get_session(self, key): return copy.deepcopy(self.sessions.get(key))

    def invited_session(self, m):
        return next((copy.deepcopy(s) for s in reversed(list(self.sessions.values())) if s['answers'].get(m) == 'asked'), None)


def venue(vid, price=25, tags=('vegetarian',), rating=4.5, cuisine='thai', reservable=None, food=True):
    return SimpleNamespace(id=f'{vid}@{SAT}', name=vid.title(), cuisine=cuisine, price=price, rating=rating,
                           distance_min=10, tags=list(tags), slot=SAT, reservable=reservable, food=food)


def two_venues(area, slot, kind='restaurants'):
    return [venue('greens'), venue('steakhouse', tags=(), rating=4.9, cuisine='steak')], [('Gone', 'closed_permanently')]


class Harness:
    def __init__(self, llm_reply=None, places=None, table=None, store=None):
        self.store = store or FakeStore()
        self.svc = Service(self.store, llm=lambda s, u: json.dumps(llm_reply or {}), places=places or two_venues,
                           check_table=table or (lambda v, s, n, city, what='': booking.Booking('ready', v, s, n, 'https://www.opentable.com/x', ('7:00 PM',), what=what)),
                           now=lambda: NOW, rng=random.Random(1))

    def say(self, member, text):
        contact = {'channel': 'imessage', 'space_id': f'space-{member}', 'agent_id': 'agent'}
        return [(c['space_id'].removeprefix('space-'), t) for c, t in self.svc.handle(member, text, contact)]

    def group(self, *people):
        """people[0] creates a group; the rest join and set their names."""
        for p, _ in people:
            self.say(p, 'hi')
        code = re.search(r'JOIN (\w+)', self.say(people[0][0], 'start group')[0][1]).group(1)
        for p, name in people:
            if p != people[0][0]:
                self.say(p, f'join {code}')
            self.say(p, f'name {name}')
        return code


MARK, EDDIE, MIKE = '+1MK', '+1ED', '+1MI'


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

    def test_organizers_agent_asks_friends_agents_then_plans(self):
        h = Harness()
        h.group((MARK, 'Mark'), (EDDIE, 'Eddie'), (MIKE, 'Mike'))
        h.store.personas[EDDIE]['hard']['dietary'] = ['vegetarian']       # private to Eddie's agent
        out = h.say(MARK, 'plan dinner saturday 7pm near Westwood with Eddie and Mike')
        self.assertEqual(sorted(to for to, _ in out), sorted([EDDIE, MARK, MIKE]))
        invite = dict(out)[EDDIE]
        self.assertEqual(invite, 'Mark wants to plan dinner Oct 24 at 7pm near Westwood. Are you in? Reply IN or OUT.')
        self.assertIn("asked Eddie and Mike's agents", dict(out)[MARK])
        out = h.say(MIKE, 'out')
        self.assertEqual(out, [(MIKE, "Got it, you're out. I let Mark know."), (MARK, 'Mike is out for dinner Oct 24 at 7pm.')])
        out = h.say(EDDIE, 'in')
        self.assertEqual(out[:2], [(EDDIE, "You're in. I'll send the plan once everyone answers."),
                                   (MARK, 'Eddie is in for dinner Oct 24 at 7pm.')])
        plans = [(to, t) for to, t in out[2:] if t.startswith('Plan for')]
        self.assertEqual(sorted(to for to, _ in plans), [EDDIE, MARK])  # Mike is out, so he isn't sent the plan
        for _, text in plans:
            self.assertIn('Greens', text)                                  # the steakhouse fails Eddie's private diet
            self.assertNotIn('vegetarian', text)                            # and nobody is told why
            self.assertIn('Your match:', text)
        self.assertEqual(out[-1], (MARK, 'Not included: Mike.'))
        self.assertIn('No open invitation', h.say(EDDIE, 'in')[0][1])

    def test_busy_friend_is_answered_by_their_agent(self):
        h = Harness()
        h.group((MARK, 'Mark'), (EDDIE, 'Eddie'))
        h.store.personas[EDDIE]['hard']['busy_days'] = ['2026-10-24']
        out = h.say(MARK, 'plan dinner sat 7pm with Eddie')
        self.assertEqual(out[0], (MARK, "Eddie's agent says Eddie can't make Oct 24 at 7pm."))
        self.assertNotIn(EDDIE, [to for to, _ in out])                     # Eddie isn't bothered
        self.assertTrue(any(t.startswith('Plan for you') for _, t in out))  # Mark still gets a plan
        self.assertEqual(out[-1], (MARK, 'Not included: Eddie.'))

    def test_unknown_names_and_go(self):
        h = Harness()
        h.group((MARK, 'Mark'), (EDDIE, 'Eddie'))
        out = h.say(MARK, 'plan dinner sat 7pm with Eddie and Zoe')
        self.assertIn('Zoe is not in group', out[0][1])
        self.assertIn('Nothing is waiting', h.say(EDDIE, 'go')[0][1])      # only the organizer can GO
        out = h.say(MARK, 'go')
        self.assertTrue(out[0][1].startswith('Plan for you'))
        self.assertEqual(out[-1], (MARK, 'Not included: Eddie.'))
        self.assertIn('No open invitation', h.say(EDDIE, 'in')[0][1])      # the invite closed with the plan

    def test_busy_reply_to_invite_saves_the_day(self):
        h = Harness()
        h.group((MARK, 'Mark'), (EDDIE, 'Eddie'))
        h.say(MARK, 'plan dinner sat 7pm with Eddie')
        h.say(EDDIE, 'busy')
        self.assertEqual(h.store.personas[EDDIE]['hard']['busy_days'], ['2026-10-24'])
        self.assertEqual(h.store.sessions[h.store.member_group(MARK)]['status'], 'planned')

    def test_movie_plan_searches_theaters_and_checks_showtimes(self):
        kinds, checks = [], []

        def places(area, slot, kind='restaurants'):
            kinds.append(kind)
            return [venue('regal', tags=(), cuisine='movie theater', food=False, price=18)], []
        h = Harness(places=places, table=lambda v, s, n, city, what='': checks.append((v, what)) or
                    booking.Booking('ready', v, s, n, 'https://amc.example/x', ('7:45 PM',), what=what))
        h.say(MARK, 'hi')
        h.store.personas[MARK]['hard']['dietary'] = ['halal']                # food rules don't veto a cinema
        out = h.say(MARK, 'plan movie: watch Primetime at 8pm next saturday near Westwood')
        self.assertEqual(kinds, ['movie theaters'])
        self.assertIn('(to watch Primetime): Regal', out[0][1])
        self.assertEqual(h.store.sessions[MARK]['event'], 'Primetime')
        self.assertIn('per ticket, estimated', out[0][1])
        out = h.say(MARK, 'book')
        self.assertIn('Checking showtimes at Regal', out[0][1])
        self.assertEqual(checks, [('Regal', 'to watch Primetime')])
        self.assertIn('Showtimes to watch Primetime at Regal', out[1][1])

    def test_plan_survives_a_restart(self):
        h = Harness()
        h.say('+1A', 'hi')
        h.say('+1A', 'plan dinner sat 7pm')
        fresh = Harness(store=h.store)                                     # new process, same Tiger store
        self.assertIn('Checking reservations', fresh.say('+1A', 'book')[0][1])

    def test_book_says_checking_first_then_tells_everyone(self):
        seen = []
        h = Harness(table=lambda v, s, n, city, what='': seen.append((v, s, n, city)) or booking.Booking('ready', v, s, n, 'https://www.opentable.com/x', ('7:00 PM',)))
        h.say('+1A', 'hi')
        self.assertIn('No plan yet', h.say('+1A', 'book')[0][1])
        h.say('+1A', 'plan dinner sat 7pm near Westwood')
        out = h.say('+1A', 'book')
        self.assertIn('Checking reservations', out[0][1])
        self.assertIn('Not booked yet', out[1][1])
        self.assertEqual(seen, [('Steakhouse', SAT, 1, 'Los Angeles')])

    def test_book_falls_back_to_runner_up(self):
        tried = []

        def table(v, s, n, city, what=''):
            tried.append(v)
            return booking.Booking('unavailable' if v == 'Steakhouse' else 'ready', v, s, n, 'https://www.opentable.com/x', ('7:15 PM',))
        h = Harness(table=table)
        h.say('+1A', 'hi')
        h.say('+1A', 'plan dinner sat 7pm')
        out = h.say('+1A', 'book')
        self.assertEqual(tried, ['Steakhouse', 'Greens'])
        self.assertIn('usually 1-3 minutes', out[0][1])
        self.assertIn('Steakhouse has no table then. Checking the runner-up, Greens', out[1][1])
        self.assertTrue(out[2][1].startswith('Tried Steakhouse first. A table for 1 at Greens'))

    def test_no_reservations_is_walk_in_not_full_and_is_remembered(self):
        tried = []

        def table(v, s, n, city, what=''):
            tried.append(v)
            return booking.Booking('walk_in' if v == 'Steakhouse' else 'ready', v, s, n, 'https://x', ())
        h = Harness(table=table)
        h.say('+1A', 'hi')
        h.say('+1A', 'plan dinner sat 7pm')
        out = h.say('+1A', 'book')
        self.assertIn("Steakhouse doesn't take reservations (walk-in). Checking the runner-up, Greens", out[1][1])
        self.assertIn('walk-in only', h.say('+1A', 'plan dinner sat 7pm')[0][1])
        tried.clear()
        h.say('+1A', 'book')
        self.assertEqual(tried, ['Greens'])

    def test_walk_in_top_pick_is_never_checked(self):
        tried = []
        places = lambda a, s, kind='restaurants': ([venue('walkin', rating=4.9, reservable=False), venue('bistro', reservable=True),
                                                    venue('cafe', reservable=False)], [])
        h = Harness(places=places, table=lambda v, s, n, city, what='': tried.append(v) or booking.Booking('ready', v, s, n, 'https://x', ()))
        h.say('+1A', 'hi')
        self.assertIn('walk-in only', h.say('+1A', 'plan dinner sat 7pm')[0][1])
        out = h.say('+1A', 'book')
        self.assertEqual(tried, ['Bistro'])
        self.assertTrue(out[-1][1].startswith('Walkin is walk-in only. A table for 1 at Bistro'))

    def test_places_failure_never_invents_a_venue(self):
        def down(area, slot, kind='restaurants'):
            raise OSError('network')
        h = Harness(places=down)
        h.say('+1A', 'hi')
        out = h.say('+1A', 'plan dinner sat 7pm')
        self.assertEqual(len(out), 1)
        self.assertIn("couldn't search restaurants", out[0][1])

    def test_no_plan_reports_categories_not_limits(self):
        h = Harness(places=lambda a, s, kind='restaurants': ([venue('pricey', price=90)], []))
        h.say('+1A', 'hi')
        h.store.personas['+1A']['hard']['max_price'] = 37
        out = h.say('+1A', 'plan dinner sat 7pm')
        self.assertIn('(budget)', out[0][1])
        self.assertNotIn('37', out[0][1])

    def test_unknown_group_code(self):
        self.assertIn("can't find group", Harness().say('+1A', 'join ZZZZZ')[0][1])


class ParseTests(unittest.TestCase):
    def test_when(self):
        self.assertEqual(parse_when('plan dinner saturday 7pm', NOW), SAT)
        self.assertEqual(parse_when('plan sat 7:30pm', NOW), '2026-10-24T19:30')
        self.assertEqual(parse_when('plan dinner', NOW), SAT)
        self.assertEqual(parse_when('plan tomorrow 8pm', NOW), '2026-10-19T20:00')
        self.assertEqual(parse_when('plan sunday 18:00', NOW), '2026-10-18T18:00')
        self.assertEqual(parse_when('plan sunday 12am', NOW), '2026-10-25T00:00')
        self.assertEqual(parse_when('watch Primetime at 8pm next saturday', NOW), '2026-10-24T20:00')

    def test_area(self):
        self.assertEqual(parse_area('plan dinner sat 7pm near Westwood'), 'Westwood, Los Angeles')
        self.assertEqual(parse_area('plan dinner in Santa Monica'), 'Santa Monica, Los Angeles')
        self.assertEqual(parse_area('plan dinner sat 7pm'), DEFAULT_AREA)
        self.assertEqual(parse_area('plan near Pasadena, CA'), 'Pasadena, CA')

    def test_with_and_activity(self):
        names, rest = parse_with('plan movie watch Primetime at 8pm next saturday with Eddie and Mike near Westwood')
        self.assertEqual(names, ['Eddie', 'Mike'])
        self.assertEqual(parse_area(rest), 'Westwood, Los Angeles')
        self.assertEqual(parse_with('plan dinner with Eddie, Mike & Zoe')[0], ['Eddie', 'Mike', 'Zoe'])
        self.assertEqual(parse_with('plan dinner sat 7pm'), ([], 'plan dinner sat 7pm'))
        self.assertEqual(parse_activity(rest), ('movie theaters', 'to watch Primetime'))
        self.assertEqual(parse_activity('plan a movie sat'), ('movie theaters', 'a movie'))
        self.assertEqual(parse_activity('plan brunch sunday'), ('restaurants', 'brunch'))
        self.assertEqual(parse_activity('plan sat 7pm'), ('restaurants', 'dinner'))


if __name__ == '__main__':
    unittest.main()
