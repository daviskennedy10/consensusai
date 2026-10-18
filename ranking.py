"""Soft-preference scoring. Pure and deterministic; hard vetoes live in constraints.py.

A feature maps (persona, candidate) to a utility in [0, 1], or None when it does not
apply to this persona (e.g. no preferred slots set). Weights are normalized over the
features that apply, so the score is always in [0, 1].
New event types (concerts, flights) pass their own feature dict to score().
"""
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

NEUTRAL_CUISINE = 0.3    # utility of a cuisine the person never rated
COMFORT_FLOOR = 0.1      # utility right at the hard max when a comfort level is set
RATING_RANGE = (3.0, 5.0)  # ratings below 3 are all "bad"; spread 3..5 over 0..1


@dataclass(frozen=True)
class Feature:
    name: str
    utility: Callable[[dict, object], Optional[float]]
    default_weight: float = 0.0  # used when the persona's weights omit this feature


def soft(persona):
    return persona.get('soft', {})


def threshold_utility(value, hard_max, comfort=None):
    """Linear 1 -> 0 up to the hard max (legacy). With a comfort level: 1 up to comfort,
    then a smoothstep decay to COMFORT_FLOOR at the hard max. Above the max is a veto
    upstream, so callers never rely on the value there."""
    if comfort is None:
        return max(0.0, 1 - value / hard_max)
    comfort = min(comfort, hard_max)
    if value <= comfort:
        return 1.0
    if value >= hard_max:
        return COMFORT_FLOOR
    t = (value - comfort) / (hard_max - comfort)
    return COMFORT_FLOOR + (1 - COMFORT_FLOOR) * (1 - t * t * (3 - 2 * t))


def cuisine_utility(persona, c, neutral=NEUTRAL_CUISINE):
    return persona['cuisine_pref'].get(c.cuisine.lower(), neutral)


def rating_utility(persona, c):
    lo, hi = RATING_RANGE
    return min(1.0, max(0.0, (c.rating - lo) / (hi - lo)))


def distance_utility(persona, c):
    return threshold_utility(c.distance_min, persona['hard']['max_distance_min'], soft(persona).get('comfort_distance_min'))


def price_utility(persona, c):
    return threshold_utility(c.price, persona['hard']['max_price'], soft(persona).get('comfort_price'))


def time_utility(persona, c):
    preferred = soft(persona).get('preferred_slots')
    return None if not preferred else float(c.slot in preferred)


# Order matters only for tie-breaking the "driven by" reason; it matches the legacy order.
RESTAURANT_FEATURES: Dict[str, Feature] = {f.name: f for f in (
    Feature('cuisine', cuisine_utility),
    Feature('rating', rating_utility),
    Feature('distance', distance_utility),
    Feature('price', price_utility),
    Feature('time', time_utility, default_weight=0.5),
)}


def score(persona, c, features=RESTAURANT_FEATURES) -> Tuple[float, Dict[str, float]]:
    """Return (score, contributions); contributions are weight-normalized and sum to score."""
    weights = persona['weights']
    utils = {}
    for name, f in features.items():
        u = f.utility(persona, c)
        if u is not None:
            utils[name] = (weights.get(name, f.default_weight), u)
    total = sum(w for w, _ in utils.values())
    if total <= 0:
        return 0.0, {name: 0.0 for name in utils}
    parts = {name: w * u / total for name, (w, u) in utils.items()}
    return sum(parts.values()), parts
