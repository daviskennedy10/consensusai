# Negotiation design (proposal, not yet wired)

The engine side lives in `negotiation.py` and `fairness.py` (pure functions, tested).
This note proposes the wire messages and agent wiring. **Nothing here is in `models.py`
until the teammate agrees.**

## Principles

- Hard constraints are never relaxed by the engine. A relaxation is only a *proposal*
  staged as the usual pending `set` update; the owner must send `confirm`.
- The organizer only learns categories (`availability`, `budget`, `distance`, `cuisine`)
  and scores. The relaxation size ("+$4 budget") is computed and shown only inside the
  owner's personal agent.
- Diet is non-negotiable: candidates vetoed for `dietary` are never offered as near
  misses, and `minimal_relaxation` returns `None` for them. Dietary checks run first,
  so a negotiable reason can never hide a dietary one.

## A. Nothing feasible: counteroffers

```
organizer                              personal agent (blocker only)        owner
   | near_misses(replies)                    |                                 |
   | counteroffers(replies, candidates)      |                                 |
   |--- CounterofferRequest ---------------->| minimal_relaxation(persona, c)  |
   |                                         |   None -> auto-decline          |
   |                                         |   dict -> stage pending update  |
   |                                         |--- question + describe_relaxation(...) -->|
   |                                         |<-- confirm / cancel ------------|
   |<-- CounterofferReply(accepted) ---------| (on confirm: validate_update + save_persona)
   | all blockers of one candidate accepted -> re-run PLAN round (fresh ScoreRequest)
```

The organizer may send counteroffers for the top 3 near misses at once; it re-plans as
soon as every blocker of one candidate accepts. It never treats acceptance as a score:
it re-broadcasts `ScoreRequest`, so the newly saved persona is evaluated as usual.

### Proposed models

```python
class CounterofferRequest(Model):
    plan_id: str
    round: int
    candidate: Candidate     # the near miss
    category: str            # generic: availability | budget | distance | cuisine
    question: str            # from negotiation.counteroffers(), safe to show the owner

class CounterofferReply(Model):
    plan_id: str
    round: int
    persona: str
    candidate_id: str
    accepted: bool           # True only after the owner confirmed; never carries values
```

Personal agent handling (teammate's file, sketch):

```python
changes = minimal_relaxation(persona, msg.candidate)
if changes is None:            # diet, or cannot be unlocked
    reply accepted=False
elif changes == {}:            # already fine (persona changed since scoring)
    reply accepted=True
else:
    ctx.storage.set(f'pending_{owner}', json.dumps(changes))   # same as a `set` command
    tell owner: msg.question + ' This needs: ' + describe_relaxation(persona, changes) + '. Reply confirm or cancel.'
    # existing confirm handler saves it; then send CounterofferReply(accepted=True)
```

## B. Feasible but someone is unhappy: compromise set and rotation

`propose_compromise(table, history, names)` returns `None` when the fair pick leaves
everyone ≥ 0.4 with spread ≤ 0.4. Otherwise it returns:

- `options`: the top 2–3 Pareto-optimal candidates in fairness order, each with a
  one-line tradeoff, e.g. `A: Med Grill: everyone ≥ 0.60 (fair pick)` /
  `B: Garden Fusion: 2 people love it, lowest is 0.33`. Good fit for an ASI:One
  `carousel` card vote.
- `rotation_credits`: if the group just takes the fair pick, store these. Whoever
  compromised (regret > 0) banks credit; next time `decide(table, history=credits)`
  prioritizes them. Credits are capped at `fairness.CREDIT_CAP` (0.1) and decay by half
  each outing, so they only break near-ties.

### Proposed models

```python
class CompromiseOption(Model):
    candidate_id: str
    line: str

class CompromiseProposal(Model):     # organizer -> group chat / card
    plan_id: str
    options: List[CompromiseOption]

class CompromiseVote(Model):         # per sender, from a carousel card or text "A"/"B"
    plan_id: str
    persona: str
    candidate_id: str
```

Vote resolution is the organizer's call (e.g. majority, falling back to the fair pick).

## Storage mapping (Tiger Data, later)

`history` is `{person: credit}`, i.e. one row per member in
`fairness_credits(group_id, member_id, credit, updated_at)`. Write
`update_credits(history, decision)` after each confirmed outing.
