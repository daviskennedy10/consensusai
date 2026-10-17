# ConsensusAI prototype (hackathon)

An ASI:One-chat-accessible organizer communicates with three independently configured personal agents. Each personal agent privately enforces deal breakers and returns only scores/vetoes. The organizer selects a maximin-fair feasible dinner proposal after **all agents** respond.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v test_consensus.py
```

Use **separate private, random seeds** for any real deployment. `personas.json` contains public demo seeds; they're not private or secure identities. Never put real private user information or payment credentials in this demo configuration.

### Local demo

Run the three personal agents in separate terminals:

```bash
export ORGANIZER_ADDRESS='<organizer agent address from organizer startup logs>'
export MAYA_OWNER_ADDRESSES='<ASI:One owner agent address for Maya>'
python personal_agent.py maya
```

Repeat for `jordan` / `sam`, setting their matching owner address environment variables and the same `ORGANIZER_ADDRESS`.

Then start the organizer:

```bash
export ORGANIZER_SEED='your-demo-organizer-seed'
python organizer.py
```

**Start organizer first to obtain its address**, then set `ORGANIZER_ADDRESS` for all personal agent terminals and launch them. On each personal agent's ASI:One chat, type `help` to see commands, then e.g. `set max_price 30`, followed by `confirm`. `show` shows locally stored preferences. Owner allowlists are mandatory: an arbitrary stranger cannot change another person's settings. Scoring requests only come from the pinned organizer address. Runtime preferences persist under ignored `data/*.json`.

Chat with the organizer via ASI:One / Agentverse Mailbox. Send `PLAN` to start the **fixed** restaurant/date scenario. The agent requires all three replies. On timeout it returns `INCOMPLETE` instead of violating someone's potentially unknown deal breakers.

## Known limitations (must be honest in demo)

- `PLAN` runs only a predefined scenario. Natural-language extraction and negotiated approval flows are **not implemented**.
- The organizer is not a fourth dinner attendee; current demo has **three personal agents**, not four.
- No live venue search or weather API. The restaurant dataset is fictional; dates are fixed at October 23–25, 2026.
- Calendar link opens a draft event only (no invites are automatically sent).
- No payments. A proposed plan does not mean all humans approved it.
- Preference edits use **structured chat commands** plus a confirmation step, not free-form LLM parsing.
- Local uAgents/ASI:One delivery must be tested against the live service. The automated tests only exercise pure Python logic.
- The scoring reply includes a short reason. Avoid placing confidential values in reasons; use generic blockers.
