"""Safe preference updates parsed from chat. No LLM is trusted to enforce invariants."""
import copy
import json
import re
from pathlib import Path

FIELDS = {"max_price", "max_distance_min", "dietary", "free_slots", "weights", "cuisine_pref",
          "never_cuisines", "comfort_price", "comfort_distance_min", "preferred_slots", "busy_days"}
ALLOWED_WEIGHT_KEYS = {"cuisine", "rating", "distance", "price"}
OPTIONAL_WEIGHT_KEYS = {"time"}
SOFT_FIELDS = ("comfort_price", "comfort_distance_min", "preferred_slots")


def validate_update(original, changes):
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
        raise ValueError("Provide valid fields: " + ", ".join(sorted(FIELDS)))
    result = copy.deepcopy(original)
    hard = result["hard"]
    for key, val in changes.items():
        if key in ("max_price", "max_distance_min", "comfort_price", "comfort_distance_min"):
            if type(val) not in (int, float) or not (0 < val <= (1000 if key.endswith("price") else 240)):
                raise ValueError(f"{key} must be a positive number in range")
            (result.setdefault("soft", {}) if key in SOFT_FIELDS else hard)[key] = val
        elif key == "dietary":
            if not isinstance(val, list) or any(not isinstance(x, str) or not x or len(x)>40 for x in val):
                raise ValueError("dietary must be a list of short strings")
            hard[key] = sorted(set(x.strip().lower() for x in val))
        elif key == "never_cuisines":
            if not isinstance(val, list) or any(not isinstance(x, str) or not x.strip() or len(x)>40 for x in val):
                raise ValueError("never_cuisines must be a list of short strings")
            hard[key] = sorted(set(x.strip().lower() for x in val))
        elif key == "free_slots":
            from datetime import datetime
            if not isinstance(val, list) or not val or any(not isinstance(x,str) for x in val):
                raise ValueError("free_slots must be a nonempty list of ISO date-times")
            for x in val:
                datetime.fromisoformat(x)
            hard[key] = val
        elif key == "busy_days":
            from datetime import date
            if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
                raise ValueError("busy_days must be a list of ISO dates")
            for x in val:
                date.fromisoformat(x)
            hard[key] = sorted(set(val))
        elif key == "preferred_slots":
            from datetime import datetime
            if not isinstance(val, list) or any(not isinstance(x,str) for x in val):
                raise ValueError("preferred_slots must be a list of ISO date-times")
            for x in val:
                datetime.fromisoformat(x)
            result.setdefault("soft", {})[key] = val
        elif key == "weights":
            if not isinstance(val, dict) or not ALLOWED_WEIGHT_KEYS <= set(val) <= ALLOWED_WEIGHT_KEYS | OPTIONAL_WEIGHT_KEYS or any(type(x) not in (int,float) or x < 0 or x > 100 for x in val.values()) or sum(val.values()) <= 0:
                raise ValueError("weights must contain cuisine, rating, distance, price (optionally time) as nonnegative numbers with positive total")
            result[key] = val
        elif key == "cuisine_pref":
            if not isinstance(val, dict) or any(not isinstance(k,str) or type(v) not in (int,float) or not 0 <= v <= 1 for k,v in val.items()):
                raise ValueError("cuisine_pref must map cuisine names to scores between 0 and 1")
            result[key] = {k.lower():v for k,v in val.items()}
    return result


def parse_update(text):
    """Explicit, auditable chat command. e.g. set max_price 30 / set dietary vegetarian,halal."""
    match = re.fullmatch(r"\s*set\s+(max_price|max_distance_min|comfort_price|comfort_distance_min|dietary|never_cuisines|free_slots|preferred_slots|weights|cuisine_pref)\s+(.+?)\s*", text, re.I|re.S)
    if not match:
        raise ValueError("Try: set max_price 25 | set dietary vegetarian,halal | set weights {\"cuisine\": 4, \"rating\": 3, \"distance\": 2, \"price\": 1}")
    key, raw = match.group(1).lower(), match.group(2).strip()
    if key in ("max_price", "max_distance_min", "comfort_price", "comfort_distance_min"):
        value = float(raw.lstrip('$'))
    elif key in ("dietary", "never_cuisines", "free_slots", "preferred_slots"):
        value = [v.strip() for v in raw.split(',') if v.strip()]
    else:
        value = json.loads(raw)
    return {key: value}


def load_persona(name, base_dir):
    """Runtime adjustments live in ignored local files, not in committed demo personas."""
    base = json.loads((Path(base_dir)/"personas.json").read_text())[name]
    path = Path(base_dir) / "data" / f"{name}.json"
    if path.exists():
        base = validate_update(base, json.loads(path.read_text()))
    return base


def save_persona(name, base_dir, persona):
    original = json.loads((Path(base_dir)/"personas.json").read_text())[name]
    delta = {**{k:persona['hard'][k] for k in ('max_price','max_distance_min','dietary','free_slots')}, 'weights':persona['weights'], 'cuisine_pref':persona['cuisine_pref']}
    if 'never_cuisines' in persona['hard']:
        delta['never_cuisines'] = persona['hard']['never_cuisines']
    delta.update({k:v for k,v in persona.get('soft', {}).items() if k in SOFT_FIELDS})
    validate_update(original, delta)
    folder = Path(base_dir)/"data"
    folder.mkdir(exist_ok=True)
    (folder/f"{name}.json").write_text(json.dumps(delta, indent=2))
