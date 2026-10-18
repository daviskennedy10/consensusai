# ConsensusAI prototype (hackathon)

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

An ASI:One-chat-accessible organizer communicates with three independently configured personal agents. Each personal agent privately enforces deal breakers and returns only scores/vetoes. The organizer selects a maximin-fair feasible dinner proposal after **all agents** respond.

## Agents

Every agent is a Fetch.ai uAgent registered on Agentverse (mailbox) and reachable through ASI:One with the Agent Chat Protocol. Each one privately scores candidate plans for its owner, and any of them can coordinate a group plan.

| Agent | Name | Address |
|---|---|---|
| Alex | `consensusai-alex` | [`agent1qf9u9z48ntupawq9vmewexygv70tmk7mtj6ejtlpur6n24gzgzajz95g4fv`](https://agentverse.ai/agents/details/agent1qf9u9z48ntupawq9vmewexygv70tmk7mtj6ejtlpur6n24gzgzajz95g4fv/profile) |
| Maya | `consensusai-maya` | [`agent1qtk2djdjqmpa7geutqgkdyy6lpshatl2m2d7m65ntndt7zfp3496ur9sn8x`](https://agentverse.ai/agents/details/agent1qtk2djdjqmpa7geutqgkdyy6lpshatl2m2d7m65ntndt7zfp3496ur9sn8x/profile) |
| Sam | `consensusai-sam` | [`agent1q0tsfuy8kpr5pp7mqa944jjlr648f9hn092khw4fpvxcalpq9l7pws8cxvw`](https://agentverse.ai/agents/details/agent1q0tsfuy8kpr5pp7mqa944jjlr648f9hn092khw4fpvxcalpq9l7pws8cxvw/profile) |
| Jordan | `consensusai-jordan` | [`agent1qvkmuvs7dnyy4gxymrfkz2htcfqkrzjsj0trtj2whq8savq8knxrj879t9q`](https://agentverse.ai/agents/details/agent1qvkmuvs7dnyy4gxymrfkz2htcfqkrzjsj0trtj2whq8savq8knxrj879t9q/profile) |

Alex (the 4th participant) and peer-coordinated planning are on `feature/agent-coordination` until that branch merges.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v      # all tests, stdlib only, ~3 s
python bench_fairness.py   # fairness vs averaging benchmark
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

## Preference engine

Pure, deterministic Python (stdlib only), so it runs and tests without agents or network.

| Module | Role |
|---|---|
| `constraints.py` | Declarative hard (deal breaker) and soft constraints. Hard ones veto and are never relaxed by the engine. Shared reasons carry only a category (`availability`, `budget`, `distance`, `dietary`, `cuisine`), never limits or diet. |
| `ranking.py` | Pluggable soft features (cuisine, rating, distance, price, preferred time). Weights normalize to 1, comfort thresholds decay smoothly to the hard max, and ratings are spread over 3-5. Other event types pass their own feature set. |
| `fairness.py` | `decide(table, objective)` with `leximin` (default), `maximin`, `nash` and `utilitarian`. It returns the pick, ranking, average pick, metrics (min, mean, spread, Gini, per-person regret) and an explanation. A bounded fairness credit prioritizes whoever compromised last time. |
| `negotiation.py` | When nothing is feasible, near-miss counteroffers go only to the blocking person. `minimal_relaxation` runs privately in that person's agent and needs the owner's `confirm`. When the fair pick is still lopsided, it builds a Pareto compromise set and rotation credits. See `docs/negotiation.md`. |

`scoring.evaluate` / `choose` / `build_complete_table` keep their signatures and delegate to these modules. `choose` now uses leximin, which always matches maximin's worst-off score and breaks its ties better.

On the demo data: *"Med Grill keeps everyone at 0.42 or higher; the highest-average option, Garden Fusion, would leave someone at 0.33."*

### Group memory (Tiger Data / Postgres)

`memory.py` is a small knowledge graph stored as plain Postgres tables, hosted on Tiger Data.
- **Nodes:** `members`, `groups`
- **Edges:** `member_of`, and `likes` (member to cuisine, with a score)
- **State:** `fairness_credits` and `plans`

It does three things:
- **Fairness over time:** confirming a plan banks credit for whoever compromised, and `get_credits()` feeds `fairness.decide(history=...)`, so the same person doesn't lose every outing.
- **No cold start:** returning members keep their shared likes, and `group_consensus()` walks group → members → cuisines.
- **Consent:** it stores only what members chose to share, never deal breakers, and `forget_member()` deletes everything about a person.

```bash
echo 'DATABASE_URL=postgres://...' > .env           # Tiger connection string; .env is gitignored
set -a; . ./.env; set +a
python memory.py init                               # create tables (idempotent)
python memory.py seed-demo                          # load the fictional demo group
TEST_DATABASE_URL="$DATABASE_URL" python -m unittest test_memory   # opt-in; uses a throwaway schema
```

### Natural-language preferences (StepFun Step 5 Preview)

**Where Step 5 Preview is called:** [`nl_preferences.py` → `stepfun_chat()`](nl_preferences.py#L74). It sends `POST https://api.stepfun.ai/v1/chat/completions` with `model: "step-5-preview"` and `reasoning_effort: "low"`. [`propose()`](nl_preferences.py#L193) is the entry point.

**What the model contributes:** people describe what they want the way they'd text a friend, and Step 5 Preview turns that into a structured preference change, which is the one thing rules can't do well.

| Input (one chat message) | Step 5 Preview output | What the owner sees, before they `confirm` |
|---|---|---|
| "I absolutely can't spend more than 30 bucks, and I'll never eat steak" | `{"max_price": 30, "never_cuisines_add": ["steak"]}` | `max_price: 25 -> 30` / `never_cuisines: + steak` |
| "I'd rather go Sunday, and I don't want to travel more than 20 min" | `{"preferred_slots_add": ["2026-10-25T18:00"], "max_distance_min": 20}` | `preferred_slots: + Oct 25 at 6pm` / `max_distance_min: 15 -> 20` |
| "Ratings matter way more to me than price" | `{"weights": {"rating": 10, "price": 2}}` | `weights.price: 14% -> 10% of the decision` |

**Users stay in control:**
- The model only *proposes*. The owner sees every change in plain words and decides with `confirm` or `cancel`.
- Deterministic code applies the change: `validate_update` re-checks every field, and unknown slots or fields are rejected one by one.
- The fairness decision itself never uses an LLM.

**Privacy:** the model sees only the sentence plus the public slots and cuisines, never the stored profile. A test checks this.

**Reliability:**
- 429 and 5xx responses are retried with backoff (1 s, then 3 s).
- 401/402/403/404 come back with StepFun's hint, for example "check the StepFun balance or voucher".
- A cut-off answer is detected.
- Any failure still leaves the plain `set ...` commands working, which skip the model.

```bash
set -a; . ./.env; set +a        # STEP_API_KEY from platform.stepfun.ai (global platform)
python nl_preferences.py sam "I'd rather go Sunday, and I don't want to travel more than 20 min"
STEPFUN_LIVE_TEST=1 python -m unittest test_nl_preferences   # opt-in live call
```

### What each part does

| Part | Role |
|---|---|
| **StepFun Step 5 Preview** | Turns natural-language preferences into structured, validated changes |
| **Fetch.ai uAgents + Agentverse + ASI:One** | Personal agents and group coordination. ASI:One is the chat interface, and Agentverse makes the agents discoverable. |
| **Preference engine** (`scoring`, `ranking`, `constraints`, `fairness`, `negotiation`) | Deterministic hard vetoes, soft scoring and leximin fairness. No LLM, so the same inputs always give the same plan. |
| **Tiger Data (Postgres)** | Group memory: shared likes and fairness credits across outings |
| **Browser Use** | Checks real table availability on OpenTable after the group approves. A dry run, so it never books. |

### Booking check (Browser Use)

After the group approves, `booking.check_availability(venue, slot, party_size)` sends a Browser Use cloud agent to check real availability. It stops at the page where a human enters their details, and **never books**. Live booking needs real approval and contact details, so it isn't built yet.

- **Bounded:** at most 25 browser steps. A real check of Water Grill (LA) for 3 people on Oct 24 at 7pm took 2 steps, 21 s and $0.012, and returned the live 6:30–7:30 PM slots.
- **Never fails silently:** a missing key, network error, blocked site, failed task or timeout returns an OpenTable deep link to finish by hand. `message(b)` gives a chat-ready line.

```bash
set -a; . ./.env; set +a        # BROWSER_USE_API_KEY
python booking.py "Water Grill" 2026-10-24T19:00 3
```

### Why fairness beats averaging

`python bench_fairness.py` simulates random groups through the real `evaluate` → `build_complete_table` → `decide` path. "Worst-off better off" means the fair pick strictly raised the least-satisfied person's score compared with picking the highest average.

500 random groups of 2-10 people per taste model, seed 0. Only groups with 2+ feasible options count (otherwise there is nothing to choose). Baseline = highest-average pick.

| Tastes | Objective | Groups | Worst-off better off | Worst-off gain (when better) | Worst-off gain (all) | Cost to mean | Gini (fair vs avg) |
|---|---|---|---|---|---|---|---|
| random | leximin | 211 | 11.8% | +0.034 | +0.004 | -0.004 | 0.064 vs 0.069 |
| random | maximin | 211 | 11.8% | +0.034 | +0.004 | -0.003 | 0.065 vs 0.069 |
| random | nash | 211 | 0.9% | +0.071 | +0.001 | -0.000 | 0.068 vs 0.069 |
| majority | leximin | 207 | 16.4% | +0.051 | +0.008 | -0.010 | 0.069 vs 0.077 |
| majority | maximin | 207 | 16.4% | +0.051 | +0.008 | -0.010 | 0.069 vs 0.077 |
| majority | nash | 207 | 1.0% | +0.083 | +0.001 | -0.000 | 0.076 vs 0.077 |

How to read it: in roughly one group in eight (random tastes), and more when one person's tastes differ from the majority's, averaging quietly sacrifices someone. Leximin fixes that while giving up only about 0.005 to 0.01 of the group's average satisfaction. Everywhere else the two pick the same plan. Nash barely differs from averaging on this data, which is why leximin is the default. The seeded test `test_simulation.py` asserts these properties on every run.

## Known limitations (must be honest in demo)

- `PLAN` runs only a predefined scenario. Natural-language extraction is **not implemented**. Counteroffer and compromise logic exists in `negotiation.py` but is **not yet wired** into the agents (see `docs/negotiation.md`).
- The organizer is not a fourth dinner attendee; current demo has **three personal agents**, not four.
- No live venue search or weather API. The restaurant dataset is fictional; dates are fixed at October 23–25, 2026.
- Calendar link opens a draft event only (no invites are automatically sent).
- No payments. A proposed plan does not mean all humans approved it.
- Preference edits use **structured chat commands** plus a confirmation step, not free-form LLM parsing.
- Local uAgents/ASI:One delivery must be tested against the live service. The automated tests only exercise pure Python logic.
- The scoring reply includes a short reason. Avoid placing confidential values in reasons; use generic blockers.
