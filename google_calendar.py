"""Owner-authorized Google Calendar free/busy access; Python stdlib only.

Connect on the owner's computer with a Desktop OAuth client. Each member has a
separate token under ignored data/google_calendar/, readable only by the OS user.
No event titles, attendees, or descriptions are requested or sent to agents.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
FREEBUSY_URL = 'https://www.googleapis.com/calendar/v3/freeBusy'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
SCOPE = 'https://www.googleapis.com/auth/calendar.freebusy'
BASE = Path(__file__).resolve().parent


class CalendarError(Exception):
    """A safe message that never contains credentials or Google's response body."""


def request_json(url, data, *, token=None, form=False):
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            value = json.loads(response.read() or b'{}')
        if not isinstance(value, dict):
            raise ValueError('Expected an object')
        return value
    except urllib.error.HTTPError as exc:
        # Do not surface OAuth responses, request headers, or URLs with tokens.
        exc.close()
        raise CalendarError(
            f'Google Calendar request failed (HTTP {exc.code}). '
            'Check that Calendar API is enabled and reconnect if authorization expired.'
        ) from None
    except (OSError, ValueError):
        raise CalendarError('Google Calendar is unavailable or returned an invalid response.') from None


def _expiry(response):
    try:
        seconds = float(response['expires_in'])
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError()
        return time.time() + seconds
    except (KeyError, TypeError, ValueError):
        raise CalendarError('Google did not return a valid token expiry. Reconnect Calendar.') from None


class CalendarClient:
    def __init__(self, base=BASE, request=request_json):
        self.directory = Path(base) / 'data' / 'google_calendar'
        self.request = request

    def token_path(self, member):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', member):
            raise CalendarError('Invalid member identifier.')
        return self.directory / (member + '.json')

    def connected(self, member):
        return self.token_path(member).is_file()

    def save(self, member, token):
        path = self.token_path(member)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        # Atomic replacement avoids partially written credentials; mkstemp is 0600.
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='token-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(token, stream)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self, member):
        try:
            value = json.loads(self.token_path(member).read_text())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise CalendarError('Calendar credentials are missing or unreadable. Reconnect Calendar.') from None

    def access_token(self, member):
        token = self.load(member)
        try:
            if token.get('access_token') and float(token.get('expires_at', 0)) > time.time() + 60:
                return token['access_token']
            fields = {k: token[k] for k in ('client_id', 'client_secret', 'refresh_token')}
            response = self.request(TOKEN_URL, {**fields, 'grant_type': 'refresh_token'}, form=True)
            if not response.get('access_token'):
                raise CalendarError('Google did not return an access token. Reconnect Calendar.')
            token.update(access_token=response['access_token'], expires_at=_expiry(response))
            if response.get('refresh_token'):
                token['refresh_token'] = response['refresh_token']
            try:
                self.save(member, token)
            except OSError:
                raise CalendarError('Calendar credentials could not be saved. Check token directory permissions.') from None
            return token['access_token']
        except (KeyError, TypeError, ValueError):
            raise CalendarError('Calendar credentials are incomplete. Reconnect Calendar.') from None

    def disconnect(self, member):
        """Revoke Google's grant before removing the local token; allow retry on failure."""
        if not self.connected(member):
            return
        token = self.load(member)
        credential = token.get('refresh_token') or token.get('access_token')
        if not credential:
            raise CalendarError('Calendar credentials are incomplete; remove the local token manually.')
        self.request(REVOKE_URL, {'token': credential}, form=True)
        self.token_path(member).unlink(missing_ok=True)

    def unavailable_slots(self, member, slots, *, timezone_name='America/Los_Angeles', duration_minutes=120):
        """Return busy candidate slots; any missing/failed calendar data raises.

        Only the primary calendar is checked. A free result supplements, and never
        overrides, the owner's manually configured availability constraints.
        """
        if not slots:
            return set()
        if not 0 < duration_minutes <= 1440:
            raise CalendarError('Event duration must be between 1 and 1440 minutes.')
        intervals = {slot: slot_interval(slot, timezone_name, duration_minutes) for slot in set(slots)}
        starts, ends = zip(*intervals.values())
        response = self.request(FREEBUSY_URL, {
            'timeMin': min(starts).isoformat(), 'timeMax': max(ends).isoformat(),
            'items': [{'id': 'primary'}],
        }, token=self.access_token(member))
        try:
            calendar = response['calendars']['primary']
            if calendar.get('errors') or not isinstance(calendar['busy'], list):
                raise ValueError()
            busy = []
            for item in calendar['busy']:
                start, end = parse_instant(item['start']), parse_instant(item['end'])
                if end <= start:
                    raise ValueError()
                busy.append((start, end))
        except (KeyError, TypeError, ValueError):
            raise CalendarError('Google Calendar availability could not be verified.') from None
        # Half-open intervals: an event ending exactly at dinner start is fine.
        return {slot for slot, (start, end) in intervals.items()
                if any(start < busy_end and busy_start < end for busy_start, busy_end in busy)}


def parse_instant(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be text')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp must have an offset')
    return dt.astimezone(timezone.utc)


def slot_interval(slot, timezone_name, duration_minutes):
    try:
        dt = datetime.fromisoformat(slot)
        if dt.tzinfo is None:
            zone = ZoneInfo(timezone_name)
            local = dt.replace(tzinfo=zone)
            # Reject ambiguous/nonexistent DST times; caller can supply an offset.
            if (local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != dt
                    or local.utcoffset() != local.replace(fold=1).utcoffset()):
                raise ValueError()
            dt = local
        start = dt.astimezone(timezone.utc)
        return start, start + timedelta(minutes=duration_minutes)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise CalendarError('Invalid or ambiguous calendar time. Use an ISO timestamp with an offset.') from None


def evaluate_candidates(member, persona, candidates, *, client, timezone_name='America/Los_Angeles'):
    """Personal-agent scoring: return only normal scores and generic vetoes.

    Network failures propagate so the agent returns an incomplete score reply.
    A disconnected calendar retains the existing manual-preferences behavior.
    """
    from scoring import evaluate
    busy = client.unavailable_slots(member, [c.slot for c in candidates], timezone_name=timezone_name) \
        if client.connected(member) else set()
    results = []
    for candidate in candidates:
        veto, score, reason = evaluate(persona, candidate)
        if not veto and candidate.slot in busy:
            veto, score, reason = True, 0.0, 'not free at that time'
        results.append((veto, score, reason))
    return results


def authorization_url(client_id, redirect_uri, state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
    return AUTH_URL + '?' + urllib.parse.urlencode({
        'client_id': client_id, 'redirect_uri': redirect_uri, 'response_type': 'code',
        'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    })


def callback_code(path, expected_state):
    """Validate the callback before any authorization code is exchanged."""
    parsed = urllib.parse.urlsplit(path)
    if parsed.path != '/oauth2callback':
        raise CalendarError('Unknown callback path.')
    query = urllib.parse.parse_qs(parsed.query)
    states = query.get('state', [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise CalendarError('OAuth state mismatch. Return to the original Google sign-in link.')
    if query.get('error'):
        raise CalendarError('Calendar access was declined. No connection was saved.')
    codes = query.get('code', [])
    if len(codes) != 1 or not codes[0]:
        raise CalendarError('Google did not return an authorization code.')
    return codes[0]


def connect(member, credentials_path, *, client, port=8765, timeout=180):
    client.token_path(member)  # Validate before starting OAuth.
    if client.connected(member):
        raise CalendarError('This member already has Calendar connected. Disconnect before replacing it.')
    try:
        credentials = json.loads(Path(credentials_path).read_text())['installed']
        client_id, client_secret = credentials['client_id'], credentials['client_secret']
        if not client_id or not client_secret:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise CalendarError('Provide the downloaded JSON for a Google OAuth Desktop app client.') from None
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # The request URL contains the authorization code.

        def do_GET(self):
            try:
                code = callback_code(self.path, state)
                result['code'] = code
                message, status = 'Sign-in received. Return to the terminal to check the connection result.', 200
            except CalendarError as exc:
                message, status = str(exc), 400
                # A forged request must not terminate a legitimate pending flow.
                if urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('state') == [state]:
                    result['error'] = message
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(message.encode())

    with HTTPServer(('127.0.0.1', port), Callback) as server:
        redirect_uri = f'http://127.0.0.1:{server.server_port}/oauth2callback'
        print(f'Connect Calendar for {member}. Open this link on the computer running this command:', flush=True)
        print(authorization_url(client_id, redirect_uri, state, verifier), flush=True)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.timeout = min(1, max(0.01, deadline - time.monotonic()))
            server.handle_request()
    if result.get('error'):
        raise CalendarError(result['error'])
    if not result.get('code'):
        raise CalendarError('Google sign-in timed out. Run connect again.')
    response = client.request(TOKEN_URL, {
        'code': result['code'], 'client_id': client_id, 'client_secret': client_secret,
        'redirect_uri': redirect_uri, 'grant_type': 'authorization_code', 'code_verifier': verifier,
    }, form=True)
    if not response.get('access_token') or not response.get('refresh_token'):
        raise CalendarError('Google did not return offline access. Reconnect and approve Calendar access.')
    if SCOPE not in response.get('scope', SCOPE).split():
        raise CalendarError('Calendar availability permission was not granted.')
    client.save(member, {
        'access_token': response['access_token'], 'refresh_token': response['refresh_token'],
        'expires_at': _expiry(response), 'client_id': client_id, 'client_secret': client_secret,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=BASE, help='Agent checkout containing data/ and personas.json')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('connect', 'status', 'disconnect', 'check'):
        sub = commands.add_parser(command)
        sub.add_argument('member', help='Personal-agent name, e.g. maya')
        if command == 'connect':
            sub.add_argument('--credentials', type=Path, default=Path('data/google-oauth-client.json'))
            sub.add_argument('--port', type=int, default=8765)
        elif command == 'check':
            sub.add_argument('slot', help='ISO start timestamp; dinner duration is two hours')
            sub.add_argument('--timezone', default=os.getenv('PLAN_TIMEZONE', 'America/Los_Angeles'))
    args = parser.parse_args()
    client = CalendarClient(args.base)
    try:
        members = json.loads((args.base / 'personas.json').read_text())
        if args.member not in members:
            raise CalendarError('Member is not in personas.json.')
        if args.command == 'connect':
            connect(args.member, args.credentials, client=client, port=args.port)
            print('Calendar connected. Planning will check your primary calendar for conflicts.')
        elif args.command == 'status':
            print('Connected locally (not a live authorization check).' if client.connected(args.member) else 'Not connected.')
        elif args.command == 'disconnect':
            client.disconnect(args.member)
            print('Calendar disconnected.')
        else:
            busy = client.unavailable_slots(args.member, [args.slot], timezone_name=args.timezone)
            print('Busy.' if busy else 'Free on the primary calendar; manual preferences still apply.')
    except (CalendarError, OSError, ValueError) as exc:
        parser.exit(1, (str(exc) if isinstance(exc, CalendarError) else 'Calendar setup could not be read or saved.') + '\n')


if __name__ == '__main__':
    main()
