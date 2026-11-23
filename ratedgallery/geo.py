"""Offline reverse geocoding (GeoNames cities1000 data shipped with reverse_geocoder)."""
from __future__ import annotations

import math
import threading

from .countries import NAMES

_rg = None
_lock = threading.Lock()


def _geocoder():
    global _rg
    with _lock:
        if _rg is None:
            import reverse_geocoder as rg

            _rg = rg.RGeocoder(mode=1, verbose=False)
    return _rg


def lookup_many(coords):
    """coords: list of (lat, lon). Returns list of dicts {cc, country, admin, city}."""
    if not coords:
        return []
    res = _geocoder().query([(float(a), float(b)) for a, b in coords])
    out = []
    for r in res:
        cc = r.get("cc") or None
        out.append({"cc": cc, "country": NAMES.get(cc, cc), "admin": r.get("admin1") or None,
                    "city": r.get("name") or None})
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1.0, a)))
