from statistics import mean
from typing import Dict, Tuple

def evaluate(persona: dict, c) -> Tuple[bool, float, str]:
    h=persona['hard']
    if c.slot not in h['free_slots']: return True,0.0,'not free at that time'
    if c.price>h['max_price']: return True,0.0,'over budget'
    if c.distance_min>h['max_distance_min']: return True,0.0,'too far'
    if any(d not in c.tags for d in h['dietary']): return True,0.0,'dietary requirement unavailable'
    w=persona['weights']
    parts={'cuisine':persona['cuisine_pref'].get(c.cuisine,0.3),'rating':c.rating/5,
           'distance':max(0,1-c.distance_min/h['max_distance_min']),
           'price':max(0,1-c.price/h['max_price'])}
    score=sum(w[k]*parts[k] for k in parts)/sum(w.values())
    best=max(parts,key=lambda k:w[k]*parts[k])
    return False,round(score,3),f'score {score:.2f}, driven by {best}'

def choose(table: Dict[str, Dict[str,float]]) -> Tuple[str,str]:
    if not table: raise ValueError('No feasible candidates')
    fair=max(table,key=lambda k:(min(table[k].values()),mean(table[k].values()),k))
    avg=max(table,key=lambda k:(mean(table[k].values()),k))
    return fair,avg

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
