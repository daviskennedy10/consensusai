"""Group memory: a small knowledge graph as plain Postgres tables (Tiger Data in prod).

Nodes: members, groups. Edges: member_of, likes (member -> cuisine, with a score).
State: fairness_credits (feeds fairness.decide(history=...)) and plans.

Consent: only what a member chose to share through their own agent is stored, never
profiles of non-users, and forget_member() deletes everything about a person.
`personas` is the personal agent service's private store (deal breakers included): only
that member's own agent reads it; group-facing code sees only scores and veto categories.
`contacts` remembers each member's chat thread so the agent can message the whole group.
shortcut: no per-role access control (row-level security); every agent with DATABASE_URL
can read every table. Add RLS or per-agent roles before real users.

CLI: python memory.py init | seed-demo   (reads DATABASE_URL)
"""
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

import fairness

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS members (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS groups (
    id text PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS member_of (
    group_id text NOT NULL REFERENCES groups ON DELETE CASCADE,
    member_id text NOT NULL REFERENCES members ON DELETE CASCADE,
    PRIMARY KEY (group_id, member_id));
CREATE TABLE IF NOT EXISTS likes (
    member_id text NOT NULL REFERENCES members ON DELETE CASCADE,
    cuisine text NOT NULL CHECK (cuisine = lower(cuisine) AND length(cuisine) BETWEEN 1 AND 40),
    score double precision NOT NULL CHECK (score BETWEEN 0 AND 1),
    PRIMARY KEY (member_id, cuisine));
CREATE TABLE IF NOT EXISTS fairness_credits (
    group_id text NOT NULL,
    member_id text NOT NULL,
    credit double precision NOT NULL CHECK (credit BETWEEN 0 AND {fairness.CREDIT_CAP}),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, member_id),
    FOREIGN KEY (group_id, member_id) REFERENCES member_of ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS personas (
    member_id text PRIMARY KEY REFERENCES members ON DELETE CASCADE,
    persona jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS contacts (
    member_id text PRIMARY KEY REFERENCES members ON DELETE CASCADE,
    channel text NOT NULL,
    space_id text NOT NULL,
    agent_id text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sessions (
    key text PRIMARY KEY,
    data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS plans (
    id text PRIMARY KEY,
    group_id text NOT NULL REFERENCES groups ON DELETE CASCADE,
    candidate_id text NOT NULL,
    objective text NOT NULL,
    metrics jsonb NOT NULL,
    explanation text NOT NULL,
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now());
"""


def connect(url=None):
    return psycopg.connect(url or os.environ['DATABASE_URL'])


def init(conn):
    with conn.transaction():
        conn.execute(SCHEMA)


def add_member(conn, member_id, display_name):
    with conn.transaction():
        conn.execute('INSERT INTO members (id, display_name) VALUES (%s, %s) '
                     'ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name', (member_id, display_name))


def create_group(conn, group_id, name, member_ids):
    with conn.transaction():
        conn.execute('INSERT INTO groups (id, name) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name',
                     (group_id, name))
        for m in member_ids:
            conn.execute('INSERT INTO member_of VALUES (%s, %s) ON CONFLICT DO NOTHING', (group_id, m))


def member_name(conn, member_id):
    row = conn.execute('SELECT display_name FROM members WHERE id = %s', (member_id,)).fetchone()
    return row[0] if row else None


def get_persona(conn, member_id):
    row = conn.execute('SELECT persona FROM personas WHERE member_id = %s', (member_id,)).fetchone()
    return row[0] if row else None


def save_persona(conn, member_id, persona):
    with conn.transaction():
        conn.execute('INSERT INTO personas (member_id, persona) VALUES (%s, %s) ON CONFLICT (member_id) '
                     'DO UPDATE SET persona = EXCLUDED.persona, updated_at = now()', (member_id, Jsonb(persona)))


def save_contact(conn, member_id, channel, space_id, agent_id):
    with conn.transaction():
        conn.execute('INSERT INTO contacts (member_id, channel, space_id, agent_id) VALUES (%s, %s, %s, %s) '
                     'ON CONFLICT (member_id) DO UPDATE SET channel = EXCLUDED.channel, space_id = EXCLUDED.space_id, '
                     'agent_id = EXCLUDED.agent_id, updated_at = now()', (member_id, channel, space_id, agent_id))


def get_contact(conn, member_id):
    row = conn.execute('SELECT channel, space_id, agent_id FROM contacts WHERE member_id = %s', (member_id,)).fetchone()
    return dict(zip(('channel', 'space_id', 'agent_id'), row)) if row else None


def member_group(conn, member_id):
    """The member's most recently created group, or None."""
    row = conn.execute('SELECT g.id FROM groups g JOIN member_of m ON m.group_id = g.id WHERE m.member_id = %s '
                       'ORDER BY g.created_at DESC LIMIT 1', (member_id,)).fetchone()
    return row[0] if row else None


def group_exists(conn, group_id):
    return conn.execute('SELECT 1 FROM groups WHERE id = %s', (group_id,)).fetchone() is not None


def latest_plan(conn, group_id):
    row = conn.execute("SELECT id, candidate_id, metrics FROM plans WHERE group_id = %s AND status = 'proposed' "
                       'ORDER BY created_at DESC LIMIT 1', (group_id,)).fetchone()
    return dict(zip(('id', 'candidate_id', 'metrics'), row)) if row else None


def save_session(conn, key, data):
    """The latest planning session for a group (or a solo member): invites, answers, the plan."""
    with conn.transaction():
        conn.execute('INSERT INTO sessions (key, data) VALUES (%s, %s) ON CONFLICT (key) '
                     'DO UPDATE SET data = EXCLUDED.data, updated_at = now()', (key, Jsonb(data)))


def get_session(conn, key):
    row = conn.execute('SELECT data FROM sessions WHERE key = %s', (key,)).fetchone()
    return row[0] if row else None


def invited_session(conn, member_id):
    """The newest session still waiting for this member's IN/OUT."""
    row = conn.execute("SELECT data FROM sessions WHERE data->'answers'->>%s = 'asked' ORDER BY updated_at DESC LIMIT 1",
                       (member_id,)).fetchone()
    return row[0] if row else None


def group_members(conn, group_id):
    return [r[0] for r in conn.execute('SELECT member_id FROM member_of WHERE group_id = %s ORDER BY member_id', (group_id,))]


def set_likes(conn, member_id, cuisine_pref):
    """Replace a member's shared cuisine likes (same shape as persona['cuisine_pref'])."""
    with conn.transaction():
        conn.execute('DELETE FROM likes WHERE member_id = %s', (member_id,))
        for cuisine, score in cuisine_pref.items():
            conn.execute('INSERT INTO likes VALUES (%s, %s, %s)', (member_id, cuisine.strip().lower(), score))


def get_likes(conn, member_id):
    return dict(conn.execute('SELECT cuisine, score FROM likes WHERE member_id = %s', (member_id,)).fetchall())


def group_consensus(conn, group_id):
    """Graph walk group -> members -> cuisines: cuisines every member rated, with the
    least-happy member's score (a maximin view), best first."""
    return conn.execute(
        'SELECT l.cuisine, min(l.score) FROM member_of m JOIN likes l ON l.member_id = m.member_id '
        'WHERE m.group_id = %s GROUP BY l.cuisine '
        'HAVING count(*) = (SELECT count(*) FROM member_of WHERE group_id = %s) '
        'ORDER BY 2 DESC, 1', (group_id, group_id)).fetchall()


def get_credits(conn, group_id):
    """{member: credit}, ready to pass as fairness.decide(history=...)."""
    return dict(conn.execute('SELECT member_id, credit FROM fairness_credits WHERE group_id = %s', (group_id,)).fetchall())


def record_plan(conn, plan_id, group_id, decision):
    with conn.transaction():
        conn.execute('INSERT INTO plans (id, group_id, candidate_id, objective, metrics, explanation) VALUES (%s, %s, %s, %s, %s, %s)',
                     (plan_id, group_id, decision.chosen, decision.objective, Jsonb(decision.metrics), decision.explanation))


def confirm_plan(conn, plan_id):
    """Mark a proposed plan as happened and bank fairness credit for whoever compromised.
    Returns False if the plan is unknown or already confirmed/cancelled (credits move once)."""
    with conn.transaction():
        row = conn.execute("UPDATE plans SET status = 'confirmed' WHERE id = %s AND status = 'proposed' "
                           'RETURNING group_id, candidate_id, objective, metrics', (plan_id,)).fetchone()
        if not row:
            return False
        group_id, chosen, objective, metrics = row
        decision = fairness.Decision(chosen, (chosen,), chosen, objective, metrics)
        credits = fairness.update_credits(get_credits(conn, group_id), decision)
        for member, credit in credits.items():
            conn.execute('INSERT INTO fairness_credits (group_id, member_id, credit) VALUES (%s, %s, %s) '
                         'ON CONFLICT (group_id, member_id) DO UPDATE SET credit = EXCLUDED.credit, updated_at = now()',
                         (group_id, member, credit))
    return True


def cancel_plan(conn, plan_id):
    with conn.transaction():
        return conn.execute("UPDATE plans SET status = 'cancelled' WHERE id = %s AND status = 'proposed'", (plan_id,)).rowcount == 1


def forget_member(conn, member_id):
    """Right to be forgotten: removes the member, their likes, memberships and credits."""
    with conn.transaction():
        return conn.execute('DELETE FROM members WHERE id = %s', (member_id,)).rowcount == 1


def seed_demo(conn, base_dir=Path(__file__).resolve().parent):
    """Load the fictional demo personas' shared likes into a 'demo' group."""
    personas = json.loads((Path(base_dir) / 'personas.json').read_text())
    for name, p in personas.items():
        add_member(conn, name, name.title())
        set_likes(conn, name, p['cuisine_pref'])
    create_group(conn, 'demo', 'Demo dinner group', list(personas))


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in ('init', 'seed-demo'):
        raise SystemExit('Usage: python memory.py init | seed-demo')
    with connect() as conn:
        init(conn)
        if sys.argv[1] == 'seed-demo':
            seed_demo(conn)
            print('demo group:', group_members(conn, 'demo'), 'consensus:', group_consensus(conn, 'demo')[:3])
        else:
            print('schema ready')
