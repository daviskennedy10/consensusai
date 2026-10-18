import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import google_calendar as gc

SLOT = '2026-10-24T19:00'
PERSONA = {
    'hard': {'max_price': 40, 'max_distance_min': 30, 'dietary': [], 'free_slots': [SLOT]},
    'cuisine_pref': {'thai': 1}, 'weights': {'cuisine': 1, 'price': 1},
}


def candidate(slot=SLOT, **updates):
    values = dict(id='dinner@' + slot, slot=slot, name='Dinner', cuisine='thai', price=20,
                  distance_min=10, tags=[], rating=4.5)
    values.update(updates)
    return SimpleNamespace(**values)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.request = Mock(return_value={'calendars': {'primary': {'busy': []}}})
        self.client = gc.CalendarClient(self.temporary.name, request=self.request)
        self.token = dict(access_token='private-access', refresh_token='private-refresh',
                          client_id='demo-client', client_secret='private-secret', expires_at=time.time() + 3600)
        self.client.save('maya', self.token)

    def test_storage_is_private_and_members_are_isolated(self):
        self.assertEqual(self.client.token_path('maya').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.client.connected('sam'))
        for name in ('../maya', '/tmp/token', '', 'maya.json/../sam'):
            with self.assertRaises(gc.CalendarError):
                self.client.token_path(name)

    def test_refresh_preserves_offline_access_and_other_members(self):
        self.client.save('sam', self.token)
        self.client.save('maya', {**self.token, 'expires_at': 0})
        self.request.return_value = {'access_token': 'refreshed', 'expires_in': 3600}
        self.assertEqual(self.client.access_token('maya'), 'refreshed')
        saved = self.client.load('maya')
        self.assertEqual(saved['refresh_token'], 'private-refresh')
        self.assertEqual(saved['client_secret'], 'private-secret')
        self.assertEqual(self.client.load('sam'), self.token)
        self.assertEqual(self.request.call_args.args[1]['grant_type'], 'refresh_token')

    def test_revocation_failure_preserves_token_for_retry(self):
        self.request.side_effect = gc.CalendarError('Unavailable')
        with self.assertRaises(gc.CalendarError):
            self.client.disconnect('maya')
        self.assertTrue(self.client.connected('maya'))
        self.request.side_effect = None
        self.client.disconnect('maya')
        self.assertFalse(self.client.connected('maya'))
        self.assertEqual(self.request.call_args.args[0], gc.REVOKE_URL)

    def test_conflicts_use_whole_duration_and_boundary_events_do_not_overlap(self):
        self.request.return_value = {'calendars': {'primary': {'busy': [
            {'start': '2026-10-25T01:00:00Z', 'end': '2026-10-25T02:00:00Z'},  # ends at dinner
            {'start': '2026-10-25T04:00:00Z', 'end': '2026-10-25T05:00:00Z'},  # starts at end
        ]}}}
        self.assertEqual(self.client.unavailable_slots('maya', [SLOT]), set())
        self.request.return_value['calendars']['primary']['busy'].append(
            {'start': '2026-10-25T03:30:00Z', 'end': '2026-10-25T03:45:00Z'})
        self.assertEqual(self.client.unavailable_slots('maya', [SLOT, SLOT]), {SLOT})
        body = self.request.call_args.args[1]
        self.assertEqual(body['timeMin'], '2026-10-25T02:00:00+00:00')
        self.assertEqual(body['timeMax'], '2026-10-25T04:00:00+00:00')
        self.assertEqual(body['items'], [{'id': 'primary'}])
        self.assertNotIn('fields', body)  # freebusy endpoint never requests event details

    def test_missing_errors_or_malformed_busy_data_never_mean_free(self):
        for payload in ({}, {'calendars': {}}, {'calendars': {'primary': {}}},
                        {'calendars': {'primary': {'errors': [{'reason': 'notFound'}], 'busy': []}}},
                        {'calendars': {'primary': {'busy': None}}},
                        {'calendars': {'primary': {'busy': [{'start': None, 'end': 'bad'}]}}},
                        {'calendars': {'primary': {'busy': [{'start': SLOT, 'end': SLOT}]}}}):
            with self.subTest(payload=payload):
                self.request.return_value = payload
                with self.assertRaises(gc.CalendarError):
                    self.client.unavailable_slots('maya', [SLOT])
        self.request.side_effect = gc.CalendarError('Network unavailable')
        with self.assertRaises(gc.CalendarError):
            gc.evaluate_candidates('maya', PERSONA, [candidate()], client=self.client)

    def test_free_calendar_cannot_override_manual_constraints(self):
        original = copy.deepcopy(PERSONA)
        results = gc.evaluate_candidates('maya', PERSONA, [candidate(price=60), candidate()], client=self.client)
        self.assertEqual(results[0], (True, 0.0, 'over budget'))
        self.assertFalse(results[1][0])
        self.assertEqual(PERSONA, original)

    def test_calendar_veto_is_generic_and_keeps_dietary_precedence(self):
        self.request.return_value = {'calendars': {'primary': {'busy': [
            {'start': '2026-10-25T02:30:00Z', 'end': '2026-10-25T03:00:00Z'}]}}}
        self.assertEqual(gc.evaluate_candidates('maya', PERSONA, [candidate()], client=self.client),
                         [(True, 0.0, 'not free at that time')])
        p = copy.deepcopy(PERSONA)
        p['hard']['dietary'] = ['vegetarian']
        self.assertEqual(gc.evaluate_candidates('maya', p, [candidate()], client=self.client),
                         [(True, 0.0, 'dietary requirement unavailable')])

    def test_disconnected_calendar_keeps_manual_behavior_without_api_calls(self):
        results = gc.evaluate_candidates('sam', PERSONA, [candidate()], client=self.client)
        self.assertFalse(results[0][0])
        self.request.assert_not_called()

    def test_timezone_and_daylight_saving_handling(self):
        start, end = gc.slot_interval('2026-12-01T19:00', 'America/Los_Angeles', 120)
        self.assertEqual(start, datetime(2026, 12, 2, 3, tzinfo=timezone.utc))
        self.assertEqual((end - start).total_seconds(), 7200)
        for slot in ('2026-03-08T02:30', '2026-11-01T01:30', 'bad'):
            with self.assertRaises(gc.CalendarError):
                gc.slot_interval(slot, 'America/Los_Angeles', 120)
        start, _ = gc.slot_interval('2026-11-01T01:30-08:00', 'America/Los_Angeles', 120)
        self.assertEqual(start.hour, 9)
        with self.assertRaises(gc.CalendarError):
            gc.slot_interval(SLOT, 'Invalid/Zone', 120)

    def test_http_errors_do_not_expose_response_credentials(self):
        for error in (urllib.error.HTTPError(gc.TOKEN_URL, 400, 'private-token', {}, io.BytesIO(b'private-secret')),
                      urllib.error.URLError('private-access')):
            with patch.object(gc.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(gc.CalendarError) as caught:
                    gc.request_json(gc.TOKEN_URL, {'secret': 'private-secret'}, form=True)
            self.assertNotIn('private-', str(caught.exception))


class OAuthTests(unittest.TestCase):
    def test_authorization_uses_pkce_state_and_only_freebusy_scope(self):
        verifier = 'x' * 64
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            gc.authorization_url('client', 'http://127.0.0.1:8765/oauth2callback', 'nonce', verifier)).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(query['code_challenge'], [expected])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['scope'], [gc.SCOPE])
        self.assertEqual(query['state'], ['nonce'])
        self.assertEqual(query['access_type'], ['offline'])

    def test_callback_rejects_forgery_denial_and_duplicate_values(self):
        self.assertEqual(gc.callback_code('/oauth2callback?state=nonce&code=ok', 'nonce'), 'ok')
        for path in ('/oauth2callback?code=ok', '/oauth2callback?state=wrong&code=ok',
                     '/other?state=nonce&code=ok', '/oauth2callback?state=nonce&error=access_denied',
                     '/oauth2callback?state=nonce&code=a&code=b',
                     '/oauth2callback?state=nonce&state=wrong&code=ok'):
            with self.assertRaises(gc.CalendarError):
                gc.callback_code(path, 'nonce')

    def test_complete_connection_persists_only_after_valid_callback(self):
        # Exercise the actual callback handler and exchange without real sockets/Google.
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / 'client.json'
            credentials.write_text(json.dumps({'installed': {'client_id': 'client', 'client_secret': 'secret'}}))
            request = Mock(return_value={'access_token': 'access', 'refresh_token': 'refresh', 'expires_in': 3600})
            client = gc.CalendarClient(directory, request=request)

            class Server:
                server_port = 8765

                def __init__(self, address, handler):
                    self.handler = handler
                    self.path = '/oauth2callback?state=nonce&code=approved'
                    self.assert_loopback = address == ('127.0.0.1', 8765)

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def handle_request(self):
                    handler = object.__new__(self.handler)
                    handler.path, handler.wfile = self.path, io.BytesIO()
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock()
                    handler.do_GET()

            with patch.object(gc, 'HTTPServer', Server), patch.object(gc.secrets, 'token_urlsafe',
                       side_effect=['nonce', 'v' * 64]), patch('builtins.print'):
                gc.connect('maya', credentials, client=client)
            token = client.load('maya')
            self.assertEqual(token['refresh_token'], 'refresh')
            exchange = request.call_args.args[1]
            self.assertEqual(exchange['code_verifier'], 'v' * 64)
            self.assertEqual(exchange['redirect_uri'], 'http://127.0.0.1:8765/oauth2callback')
            self.assertEqual(exchange['code'], 'approved')


if __name__ == '__main__':
    unittest.main()
