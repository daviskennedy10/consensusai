from typing import Dict, Tuple

import constraints
import fairness
import ranking

def evaluate(persona: dict, c) -> Tuple[bool, float, str]:
    blocked=constraints.first_violation(persona,c)
    if blocked: return True,0.0,blocked.reason
    score,parts=ranking.score(persona,c)
    best=max(parts,key=parts.get)
    return False,round(score,3),f'score {score:.2f}, driven by {best}'

def choose(table: Dict[str, Dict[str,float]]) -> Tuple[str,str]:
    """(fair_id, average_id). Leximin refines the legacy maximin: same worst-off score, better ties."""
    d=fairness.decide(table)
    return d.chosen,d.average_id

def valid_full_reply(reply, candidate_ids):
    ids=[s.candidate_id for s in reply.scores]
    return len(ids)==len(candidate_ids) and set(ids)==candidate_ids and all(0<=s.score<=1 for s in reply.scores)

def build_complete_table(replies,candidates,personas):
    """No feasible option may be selected unless ALL agents supplied all scores."""
    if set(replies)!=set(personas):
        return None
    ids={c.id for c in candidates}
    if not all(valid_full_reply(r,ids) for r in replies.values()):
        return None
    table={}
    for cid in ids:
        scores={p:next(s for s in r.scores if s.candidate_id==cid) for p,r in replies.items()}
        if not any(s.veto for s in scores.values()):
            table[cid]={p:s.score for p,s in scores.items()}
    return table
