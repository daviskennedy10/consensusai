"""iMessage bridge via Photon v2 (HTTP API, stdlib only). Run: python photon_bridge.py

Polls Photon for received iMessages and replies in the same direct space. For now it
echoes; `handle(text, sender)` is the single place to plug in the ConsensusAI back end.
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


def handle(text, sender):
    """The reply for one inbound message. Swap in the ConsensusAI back end here."""
    return f'ConsensusAI is connected. You said: {text}'


def reply(project, key, m, text):
    request('POST', f'/projects/{project}/messages', key=key, headers={'idempotency-key': f'consensusai-reply:{m["id"]}'},
            body={'spaceId': m['space']['id'], 'senderId': m['receivedBy']['id'],
                  'content': {'type': 'text', 'format': 'plain', 'text': text}})


def problem(e):
    """Photon's error code (e.g. SPACE_NOT_SENDABLE) from an HTTPError, never the request."""
    try:
        return json.loads(e.read() or b'{}').get('code') or ''
    except ValueError:
        return ''


def run(project, key, sleep=time.sleep, stop=lambda: False):
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
                    try:
                        reply(project, key, m, handle(text, sender))
                    except urllib.error.HTTPError as e:
                        if e.code in (429, 500, 502, 503, 504):
                            raise  # transient: not marked seen, so the next poll retries (idempotency key dedupes)
                        seen.add(m['id'])
                        print(f'out {mask(sender)}: reply refused, HTTP {e.code} {problem(e)}', file=sys.stderr, flush=True)
                        continue
                    seen.add(m['id'])
                    print(f'out {mask(sender)}: reply accepted by Photon', flush=True)
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
    run(os.environ['PROJECT_ID'], os.environ['PROJECT_SECRET'])
