import os
import unittest
from datetime import datetime

import venues
from venues import open_at, search

SAT_7PM = '2026-10-24T19:00'  # a Saturday
DINNER = {'periods': [{'open': {'day': d, 'hour': 17, 'minute': 0}, 'close': {'day': d, 'hour': 22, 'minute': 0}} for d in range(7)]}
LATE = {'periods': [{'open': {'day': 6, 'hour': 20, 'minute': 0}, 'close': {'day': 0, 'hour': 2, 'minute': 0}}]}  # Sat 8pm-Sun 2am
LUNCH = {'periods': [{'open': {'day': d, 'hour': 11, 'minute': 0}, 'close': {'day': d, 'hour': 15, 'minute': 0}} for d in range(7)]}


def place(pid, status='OPERATIONAL', hours=DINNER, **kw):
    return {'id': pid, 'displayName': {'text': pid.title()}, 'primaryType': 'thai_restaurant', 'rating': 4.5,
            'priceLevel': 'PRICE_LEVEL_MODERATE', 'businessStatus': status, 'regularOpeningHours': hours,
            'location': {'latitude': 34.0635, 'longitude': -118.4455}, **kw}


class OpenHoursTests(unittest.TestCase):
    def test_regular_and_overnight_hours(self):
        self.assertTrue(open_at(DINNER, datetime(2026, 10, 24, 19)))
        self.assertFalse(open_at(DINNER, datetime(2026, 10, 24, 23)))
        self.assertTrue(open_at(LATE, datetime(2026, 10, 25, 1, 30)))   # Sunday 1:30am, carried over from Saturday
        self.assertFalse(open_at(LATE, datetime(2026, 10, 24, 19)))
        self.assertFalse(open_at(LUNCH, datetime(2026, 10, 24, 19)))

    def test_24h_and_unknown_hours(self):
        self.assertTrue(open_at({'periods': [{'open': {'day': 0, 'hour': 0, 'minute': 0}}]}, datetime(2026, 10, 24, 3)))
        self.assertFalse(open_at(None, datetime(2026, 10, 24, 19)))     # unknown = not verified
        self.assertFalse(open_at({}, datetime(2026, 10, 24, 19)))


class SearchTests(unittest.TestCase):
    def fake(self, places, halal=()):
        def request(body, mask):
            if mask == 'places.location':
                return {'places': [{'location': {'latitude': 34.0635, 'longitude': -118.4455}}]}
            return {'places': list(halal if body['textQuery'].startswith('halal') else places)}
        return request

    def test_only_operational_and_open_venues_survive(self):
        kept, dropped = search('Westwood, Los Angeles', SAT_7PM, self.fake([
            place('open'), place('gone', status='CLOSED_PERMANENTLY'), place('paused', status='CLOSED_TEMPORARILY'),
            place('lunch', hours=LUNCH), place('nohours', hours=None)]))
        self.assertEqual([c.name for c in kept], ['Open'])
        self.assertEqual(dict(dropped), {'Gone': 'closed_permanently', 'Paused': 'closed_temporarily',
                                         'Lunch': 'closed at that time', 'Nohours': 'closed at that time'})

    def test_candidate_mapping(self):
        far = place('far', location={'latitude': 34.10, 'longitude': -118.33}, servesVegetarianFood=True)
        kept, _ = search('Westwood', SAT_7PM, self.fake([place('near'), far]))
        near, far = kept
        self.assertEqual((near.cuisine, near.price, near.rating, near.slot), ('thai', 30, 4.5, SAT_7PM))
        self.assertEqual(near.id, f'near@{SAT_7PM}')
        self.assertEqual(near.distance_min, 1)
        self.assertGreater(far.distance_min, 25)
        self.assertEqual(far.tags, ['vegetarian'])
        self.assertNotIn('halal', near.tags + far.tags)  # never assumed

    def test_halal_only_from_googles_halal_search(self):
        kept, _ = search('Westwood', SAT_7PM, self.fake([place('both'), place('plain')], halal=[place('both'), place('kabab')]))
        self.assertEqual({c.name: c.tags for c in kept}, {'Both': ['halal'], 'Plain': [], 'Kabab': ['halal']})

    def test_missing_key(self):
        old = os.environ.pop('GOOGLE_PLACES_API_KEY', None)
        try:
            with self.assertRaises(RuntimeError):
                search('Westwood', SAT_7PM)
        finally:
            if old is not None:
                os.environ['GOOGLE_PLACES_API_KEY'] = old


@unittest.skipUnless(os.environ.get('PLACES_LIVE_TEST') and os.environ.get('GOOGLE_PLACES_API_KEY'), 'set PLACES_LIVE_TEST=1')
class LivePlacesTests(unittest.TestCase):
    def test_real_westwood_search(self):
        kept, dropped = search('Westwood, Los Angeles', SAT_7PM)
        self.assertTrue(kept)
        self.assertTrue(all(c.name and c.rating > 0 for c in kept))


if __name__ == '__main__':
    unittest.main()
