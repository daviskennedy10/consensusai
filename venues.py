"""Real venues from Google Places (API New), verified before anyone sees them.

The rule that fixes the "recommended a restaurant that closed years ago" failure: every
candidate comes from a live Places result made at plan time, its businessStatus must be
OPERATIONAL, and it must be open at the planned slot. Nothing is cached, and no model ever
invents a venue. Unknown facts count against the venue: halal is never tagged (Places has
no such field), so a halal deal breaker vetoes until a menu check proves otherwise.
shortcut: price per person and travel minutes are estimates from Places' price tier and
straight-line distance; replace with menu prices and a routes API for production.
"""
import json
import math
import os
import urllib.request
from datetime import datetime
from types import SimpleNamespace

SEARCH_URL = 'https://places.googleapis.com/v1/places:searchText'
FIELDS = ('places.id,places.displayName,places.primaryType,places.types,places.rating,places.userRatingCount,'
          'places.priceLevel,places.businessStatus,places.regularOpeningHours,places.servesVegetarianFood,'
          'places.location,places.formattedAddress,places.googleMapsUri')
PRICE_PER_PERSON = {'PRICE_LEVEL_FREE': 0, 'PRICE_LEVEL_INEXPENSIVE': 15, 'PRICE_LEVEL_MODERATE': 30,
                    'PRICE_LEVEL_EXPENSIVE': 55, 'PRICE_LEVEL_VERY_EXPENSIVE': 90}
CITY_KMH = 20  # LA driving incl. parking; turns straight-line km into minutes


def http(body, field_mask):
    key = os.environ.get('GOOGLE_PLACES_API_KEY')
    if not key:
        raise RuntimeError('GOOGLE_PLACES_API_KEY is not set')
    req = urllib.request.Request(SEARCH_URL, data=json.dumps(body).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'X-Goog-Api-Key': key, 'X-Goog-FieldMask': field_mask})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def open_at(hours, dt):
    """True only if Places' weekly periods say the venue is open at local datetime dt."""
    periods = (hours or {}).get('periods')
    if not periods:
        return False  # unknown hours: not verified, so not proposed
    week = 7 * 24 * 60
    t = ((dt.weekday() + 1) % 7) * 1440 + dt.hour * 60 + dt.minute  # Places: 0 = Sunday
    for p in periods:
        o = p.get('open')
        if not o:
            continue
        if 'close' not in p:
            return True  # open 24 hours
        start = o['day'] * 1440 + o.get('hour', 0) * 60 + o.get('minute', 0)
        c = p['close']
        end = c['day'] * 1440 + c.get('hour', 0) * 60 + c.get('minute', 0)
        length = (end - start) % week
        if (t - start) % week < length:
            return True
    return False


def km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a['latitude'], a['longitude'], b['latitude'], b['longitude']))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def cuisine(place):
    t = place.get('primaryType') or 'restaurant'
    return t.removesuffix('_restaurant').replace('_', ' ') if t != 'restaurant' else 'restaurant'


def to_candidate(place, slot, center):
    tags = ['vegetarian'] if place.get('servesVegetarianFood') else []
    return SimpleNamespace(
        id=f"{place['id']}@{slot}", name=place['displayName']['text'], cuisine=cuisine(place),
        price=PRICE_PER_PERSON.get(place.get('priceLevel'), 30), rating=float(place.get('rating') or 0),
        distance_min=max(1, round(km(center, place['location']) / CITY_KMH * 60)) if center and place.get('location') else 15,
        tags=tags, slot=slot, address=place.get('formattedAddress', ''), maps_url=place.get('googleMapsUri', ''))


def search(area, slot, request=http, limit=15):
    """Verified candidates near `area` for `slot` (ISO local time), plus what was dropped and why."""
    dt = datetime.fromisoformat(slot)
    center = (request({'textQuery': area, 'maxResultCount': 1}, 'places.location').get('places') or [{}])[0].get('location')
    places = request({'textQuery': f'restaurants in {area}', 'maxResultCount': limit}, FIELDS).get('places', [])
    kept, dropped = [], []
    for p in places:
        name = p.get('displayName', {}).get('text', '?')
        if p.get('businessStatus') != 'OPERATIONAL':
            dropped.append((name, (p.get('businessStatus') or 'status unknown').lower()))
        elif not open_at(p.get('regularOpeningHours'), dt):
            dropped.append((name, 'closed at that time'))
        else:
            kept.append(to_candidate(p, slot, center))
    return kept, dropped
