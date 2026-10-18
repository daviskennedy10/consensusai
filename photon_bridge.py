"""iMessage bridge via Photon v2 (HTTP API, stdlib only). Run: python photon_bridge.py

Polls Photon for received iMessages and passes each one to a "brain": ConsensusAI
(consensus_service) when DATABASE_URL is set, otherwise an echo. A brain yields
(contact, text) replies, which may go to the sender or to other group members.
Shared-line rules (Photon): direct chats only, and only to registered users who texted first.
Replies are plain messages in the same space: a parentMessageId (threaded reply) gets
409 SPACE_NOT_SENDABLE on shared iMessage lines.
Env: PROJECT_ID, PROJECT_SECRET (pho_sk_ Bearer key). Phone numbers are masked in logs.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = 'https://api.photon.codes/v1'
POLL_S = 2


def mask(handle):
    return f'{handle[:2]}•••{handle[-2:]}' if handle and len(handle) > 4 else '•••'


def request(method, path, body=None, headers=None, key=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
                                          'User-Agent': 'consensusai-bridge/1.0', **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')


def text_of(m):
    c = m.get('content') or {}
    return c.get('text', '').strip() if c.get('type') == 'text' else None


def is_inbound_dm(m):
    sender, agent, space = m.get('sentBy') or {}, m.get('receivedBy') or {}, m.get('space') or {}
    return (m.get('platform') == 'imessage' and m.get('status') == 'received' and sender.get('kind') == 'human'
            and agent.get('kind') == 'agent' and space.get('kind') == 'direct' and sender.get('id') != agent.get('id'))


def echo(member, text, contact):
    yield contact, f'ConsensusAI is connected. You said: {text}'


def send(project, key, contact, text, idempotency_key):
    request('POST', f'/projects/{project}/messages', key=key, headers={'idempotency-key': idempotency_key},
            body={'spaceId': contact['space_id'], 'senderId': contact['agent_id'],
                  'content': {'type': 'text', 'format': 'plain', 'text': text}})


def problem(e):
    """Photon's error code (e.g. SPACE_NOT_SENDABLE) from an HTTPError, never the request."""
    try:
        return json.loads(e.read() or b'{}').get('code') or ''
    except ValueError:
        return ''


def run(project, key, sleep=time.sleep, stop=lambda: False, brain=echo):
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    seen, backoff = set(), POLL_S
    print(f'bridge ready: polling project {project[:12]}… every {POLL_S}s', flush=True)
    while not stop():
        try:
            query = f'createdSince={since}&direction=received&order=asc&pageSize=100&platform=imessage'
            token = None
            while True:
                page = request('GET', f'/projects/{project}/messages?{query}' + (f'&pageToken={token}' if token else ''), key=key)
                for m in page.get('items', []):
                    since = max(since, m.get('createdAt') or since)  # createdSince is inclusive; `seen` drops repeats
                    if m.get('id') in seen or not is_inbound_dm(m):
                        continue
                    text, sender = text_of(m), m['sentBy'].get('handle', '')
                    if not text:
                        seen.add(m['id'])
                        print(f'skip non-text message from {mask(sender)}', flush=True)
                        continue
                    print(f'in  {mask(sender)}: {len(text)} chars', flush=True)
                    contact = {'channel': 'imessage', 'space_id': m['space']['id'], 'agent_id': m['receivedBy']['id']}
                    sent = 0
                    try:
                        for i, (to, out) in enumerate(brain(sender, text, contact)):
                            send(project, key, to, out, f'consensusai:{m["id"]}:{i}')
                            sent += 1
                    except urllib.error.HTTPError as e:
                        if e.code in (429, 500, 502, 503, 504):
                            raise  # transient: not marked seen; the retry re-sends with the same keys, Photon dedupes
                        print(f'out {mask(sender)}: reply refused, HTTP {e.code} {problem(e)}', file=sys.stderr, flush=True)
                    except Exception as e:  # a brain bug must not kill the bridge or leave the person hanging
                        print(f'brain error {type(e).__name__}: {e}', file=sys.stderr, flush=True)
                        send(project, key, contact, 'Sorry, something went wrong on my side. Please try again.',
                             f'consensusai:{m["id"]}:error')
                    seen.add(m['id'])
                    print(f'out {mask(sender)}: {sent} message(s) accepted by Photon', flush=True)
                token = page.get('nextPageToken')
                if not isinstance(token, str):
                    break
            backoff = POLL_S
        except urllib.error.HTTPError as e:
            backoff = min(backoff * 2, 60) if e.code in (429, 500, 502, 503, 504) else backoff
            print(f'photon HTTP {e.code}; retrying in {backoff}s', file=sys.stderr, flush=True)
            if e.code in (401, 403):
                raise SystemExit('Photon rejected the credentials (check PROJECT_SECRET)')
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            backoff = min(backoff * 2, 60)
            print(f'network error {type(e).__name__}; retrying in {backoff}s', file=sys.stderr, flush=True)
        sleep(backoff)


if __name__ == '__main__':
    missing = [k for k in ('PROJECT_ID', 'PROJECT_SECRET') if not os.environ.get(k)]
    if missing:
        raise SystemExit(f'Missing {", ".join(missing)} (set -a; . ./.env; set +a)')
    brain = echo
    if os.environ.get('DATABASE_URL'):
        import consensus_service
        brain = consensus_service.from_env().handle
    print(f'brain: {"consensusai" if brain is not echo else "echo"}', flush=True)
    run(os.environ['PROJECT_ID'], os.environ['PROJECT_SECRET'], brain=brain)
