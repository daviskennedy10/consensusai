import json
import os
import unittest
import urllib.error

import booking
from booking import check_availability, message

SLOT = '2026-10-24T19:00'


class FakeAPI:
    def __init__(self, statuses, output=None, create_error=None):
        self.statuses, self.output, self.create_error, self.calls = list(statuses), output, create_error, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'POST':
            if self.create_error:
                raise self.create_error
            return {'id': 't1', 'sessionId': 's1'}
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        out = json.dumps(self.output) if isinstance(self.output, dict) else self.output
        return {'id': 't1', 'status': status, 'output': out if status == 'finished' else None, 'cost': '0.07'}


def run(api, **kw):
    return check_availability('Water Grill', SLOT, 3, request=api, sleep=lambda s: None, **kw)


class BookingTests(unittest.TestCase):
    def test_available_slot_is_ready_but_not_booked(self):
        api = FakeAPI(['created', 'started', 'finished'],
                      {'outcome': 'available', 'bookable_times': ['6:45 PM', '7:15 PM'],
                       'booking_url': 'https://www.opentable.com/booking/details?rid=1'})
        b = run(api)
        self.assertEqual((b.status, b.task_id, b.cost), ('ready', 't1', '0.07'))
        self.assertEqual(b.link, 'https://www.opentable.com/booking/details?rid=1')
        self.assertIn('Not booked yet', message(b))
        self.assertIn('6:45 PM', message(b))

    def test_task_is_a_bounded_dry_run(self):
        api = FakeAPI(['finished'], {'outcome': 'unavailable'})
        run(api)
        body = api.calls[0][2]
        self.assertIn('NEVER click a final reserve', body['task'])
        self.assertIn('NEVER enter a name, phone, email or card', body['task'])
        self.assertEqual(body['maxSteps'], booking.MAX_STEPS)
        self.assertEqual(body['metadata']['mode'], 'dry_run')
        self.assertTrue(body['startUrl'].startswith('https://www.opentable.com/s?'))
        self.assertIn('covers=3', body['startUrl'])

    def test_unavailable_reports_nearby_times(self):
        b = run(FakeAPI(['finished'], {'outcome': 'unavailable', 'bookable_times': ['9:30 PM']}))
        self.assertEqual(b.status, 'unavailable')
        self.assertIn('9:30 PM', message(b))

    def test_no_reservations_means_walk_in(self):
        b = run(FakeAPI(['finished'], {'outcome': 'no_reservations', 'notes': 'walk-in only'}))
        self.assertEqual(b.status, 'walk_in')
        self.assertEqual(message(b), "Water Grill doesn't take reservations: just walk in.")
        self.assertIn('no_reservations', booking.RESULT_SCHEMA)

    def test_every_failure_falls_back_to_a_link(self):
        cases = {
            'network': FakeAPI(['finished'], create_error=urllib.error.URLError('down')),
            'failed': FakeAPI(['failed']),
            'stopped': FakeAPI(['stopped']),
            'garbage': FakeAPI(['finished'], 'I could not do it'),
            'blocked': FakeAPI(['finished'], {'outcome': 'blocked', 'notes': 'captcha'}),
            'not_found': FakeAPI(['finished'], {'outcome': 'not_found'}),
        }
        for name, api in cases.items():
            b = run(api)
            self.assertEqual(b.status, 'fallback', name)
            self.assertTrue(b.link.startswith('https://www.opentable.com/s?'), name)
            self.assertIn(b.link, message(b))

    def test_timeout_falls_back(self):
        b = run(FakeAPI(['started']), timeout_s=9)
        self.assertEqual(b.status, 'fallback')
        self.assertIn('timed out', b.notes)

    def test_missing_key_falls_back(self):
        old = os.environ.pop('BROWSER_USE_API_KEY', None)
        try:
            b = check_availability('Water Grill', SLOT, 3, sleep=lambda s: None)
        finally:
            if old is not None:
                os.environ['BROWSER_USE_API_KEY'] = old
        self.assertEqual(b.status, 'fallback')

    def test_untrusted_urls_are_replaced(self):
        b = run(FakeAPI(['finished'], {'outcome': 'available', 'booking_url': 'javascript:alert(1)'}))
        self.assertTrue(b.link.startswith('https://www.opentable.com/s?'))

    def test_party_size_validated(self):
        for size in (0, 21):
            with self.assertRaises(ValueError):
                check_availability('X', SLOT, size, request=FakeAPI(['finished']))


if __name__ == '__main__':
    unittest.main()
