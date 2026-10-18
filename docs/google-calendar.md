# Google Calendar setup

Each personal agent can check its owner's primary Google Calendar before scoring
a plan. The connection requests **free/busy access only** (`calendar.freebusy`),
not event titles or descriptions. It does not create events or send invitations.
Existing calendar links still open drafts for people to save themselves.

## One-time Google Cloud configuration

A Places API key cannot authorize private Calendar access or administer your Cloud
project. An authorized project administrator must complete these steps:

1. Select the existing project in [Google Cloud Console](https://console.cloud.google.com/).
2. Under **APIs & Services → Library**, enable **Google Calendar API**.
3. Under **Google Auth platform → Branding**, configure the app name and support email.
4. Under **Audience**, choose **External** for personal Gmail accounts; in Testing,
   add each teammate's Google email as a test user. Internal is only for eligible
   Workspace organizations.
5. Under **Data Access**, add `https://www.googleapis.com/auth/calendar.freebusy`.
6. Under **Clients**, create a **Desktop app** OAuth client. Download its JSON and
   save it as `data/google-oauth-client.json` in this checkout. `data/` is ignored.

This demo uses Google's supported Desktop loopback OAuth flow with PKCE and a
random state. It is not a hosted web onboarding flow. A production web connection
should use a Web application client with a registered HTTPS callback and bind the
connection to the authenticated owner's session.

## Connect one member

Run this on the owner's computer, from the checkout that runs the agents:

```bash
python google_calendar.py connect maya --credentials data/google-oauth-client.json
```

Open the printed Google link, select **Maya's own Google account**, and approve
availability access. Google returns to `http://127.0.0.1:8765/oauth2callback`.
The terminal reports whether the connection succeeded. Repeat for each member,
using the matching name from `personas.json`. `--port` can choose another port.

For agents on a remote host, run this command there and forward its loopback port
to your computer over SSH before opening the link (for example,
`ssh -L 8765:127.0.0.1:8765 user@your-agent-server`). Do not expose the callback port
publicly. The CLI is an operator setup tool: selecting the correct member/account
is the operator and owner's responsibility, not an automatically verified identity
link. Credentials live in the agent checkout where the command runs.

Tokens are separate files at `data/google_calendar/<member>.json`; the directory
is mode `0700` and files are mode `0600`, with atomic writes. They include a refresh
token, so keep them private and out of git. Other users of the same operating-system
account can access them. Do not paste credentials or callback URLs into ASI:One.

## Verify and use

```bash
python google_calendar.py status maya
python google_calendar.py check maya 2026-10-24T19:00
python personal_agent.py maya
```

`status` checks whether a local connection is saved. `check` makes a live request
and reports whether the next two hours conflict with the primary calendar.

In a private ASI:One chat, @mention the personal agent and send `calendar status`
or `calendar disconnect`. The existing owner allowlist protects these commands.
After connection, `plan` automatically checks Calendar during personal-agent scoring.
Both the legacy organizer and peer coordinators receive only generic vetoes/scores.
No calendar-specific values, busy intervals, or event details are sent to peers.

Set `PLAN_TIMEZONE` to an IANA timezone if needed; default is `America/Los_Angeles`.
Naive candidate times are interpreted in that timezone. Explicit UTC offsets are
respected. Ambiguous or nonexistent daylight-saving times are rejected rather than
guessed. Each plan is treated as two hours long.

Manual availability constraints still apply: a free Calendar slot does not override
`free_slots` or `busy_days`. Without a connection the original manual behavior stays
available. With a saved connection, an API failure, denied access, or malformed reply
produces incomplete scores; the coordinator makes no group decision. Calendar
conflicts cannot be overridden by a preference counteroffer.

Only the primary calendar is checked, and availability is a snapshot taken during
scoring. This does not reserve time, check secondary calendars, or guarantee that
the slot will remain free. Group approval and a fresh check before any future
booking are separate requirements.

## Disconnect and troubleshooting

```bash
python google_calendar.py disconnect maya
```

This revokes the Google grant and then deletes the local token. If revocation fails,
the token stays so the operation can be retried. You can also remove access from
[Google Account connections](https://myaccount.google.com/connections).
Revocation can affect other connections granted through the same OAuth client and
Google account, so use a different Google account for each represented member.

If Google rejects sign-in, check the test-user list, OAuth client type, and requested
scope. If API calls fail, confirm that Calendar API is enabled in the OAuth client's
project. Google refresh tokens for an External app in Testing generally expire
after seven days for this scope; reconnect when needed. No dependencies beyond
the existing project's Python environment are required.

References: [Calendar quickstart](https://developers.google.com/workspace/calendar/api/quickstart/python),
[free/busy API](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query),
[Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).
