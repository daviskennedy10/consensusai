# ConsensusAI prototype (hackathon)

An ASI:One-chat-accessible organizer communicates with three independently configured personal agents. Each personal agent privately enforces deal breakers and returns only scores/vetoes. The organizer selects a maximin-fair feasible dinner proposal after **all agents** respond.

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
