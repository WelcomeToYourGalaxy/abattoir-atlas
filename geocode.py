"""
Geocoding.

The rule that shapes this module: a failed geocode produces no coordinate. It
never falls back to a town centroid dressed up as a facility location, and it
never falls back to a country centroid at all. Records that cannot be placed
are counted, listed, and exported for manual work -- they are not scattered
across a map as if they were known.

Precision is recorded alongside every coordinate and travels into the output,
so the map can draw rooftop and street hits as pins and hold everything coarser
out of the geometry entirely.

Providers are pluggable. Nominatim is included because it is free and open, but
its usage policy caps you at one request per second and expects a real contact
address in the User-Agent -- at 60k records that is roughly 17 hours, so run it
overnight or pay for a bulk provider. The cache means you only do it once.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from normalize import norm_text

@dataclass
class GeoHit:
    lat: float | None
    lon: float | None
    precision: str
    provider: str
    payload: dict | None = None


class Cache:
    """Gzipped JSON, keyed by normalized query string.

    Deliberately a single portable file rather than a database. It has to
    survive being committed back to a repository between CI runs, and a 5 MB
    gzipped blob does that where a SQLite file does not: the whole point of the
    cache is that seventeen hours of geocoding only ever happens once.
    """

    def __init__(self, path: str = "work/geocache.json.gz"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._d: dict[str, dict] = {}
        self._dirty = 0
        if self.path.exists():
            with gzip.open(self.path, "rt", encoding="utf-8") as fh:
                self._d = json.load(fh)

    def __len__(self):
        return len(self._d)

    def get(self, q: str) -> GeoHit | None:
        r = self._d.get(q)
        return GeoHit(r["lat"], r["lon"], r["precision"], r["provider"]) if r else None

    def put(self, q: str, hit: GeoHit) -> None:
        self._d[q] = {"lat": hit.lat, "lon": hit.lon,
                      "precision": hit.precision, "provider": hit.provider}
        self._dirty += 1
        if self._dirty >= 250:          # flush often; CI jobs get killed
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(self._d, fh, separators=(",", ":"))
        tmp.replace(self.path)
        self._dirty = 0


def build_query(rec) -> str:
    """One address string per record, normalized so that two records with the
    same address share a cache entry and cost one lookup between them."""
    parts = [rec.address, rec.locality, rec.admin1, rec.postcode, rec.country_iso3]
    return norm_text(", ".join(p for p in parts if p))


# --- Nominatim -------------------------------------------------------------

# Which OSM place classes count as a real address hit rather than a settlement.
_ROOFTOP = {"building", "amenity", "shop", "industrial", "man_made", "office",
            "landuse", "craft"}
_STREET = {"highway", "place:house_number", "address"}


def nominatim(query: str, *, contact: str, base: str = "https://nominatim.openstreetmap.org",
              pause: float = 1.1) -> GeoHit:
    url = base + "/search?" + urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1,
    })
    req = urllib.request.Request(url, headers={
        "User-Agent": f"abattoir-atlas/1.0 ({contact})",
        "Accept-Language": "en",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:                       # network, rate limit, malformed
        return GeoHit(None, None, "none", "nominatim", {"error": str(exc)})
    finally:
        time.sleep(pause)

    if not data:
        return GeoHit(None, None, "none", "nominatim", None)

    top = data[0]
    cls, typ = top.get("category") or top.get("class"), top.get("type")
    addr = top.get("address", {})

    if addr.get("house_number") or cls in _ROOFTOP:
        precision = "rooftop"
    elif cls in _STREET or addr.get("road"):
        precision = "street"
    elif typ in {"city", "town", "village", "hamlet", "suburb"}:
        precision = "locality"
    else:
        precision = "admin"

    return GeoHit(float(top["lat"]), float(top["lon"]), precision, "nominatim", top)


# --- driver ----------------------------------------------------------------

def geocode_records(records, *, contact: str, cache_path: str = "work/geocache.json.gz",
                    provider=nominatim, limit: int | None = None,
                    progress_every: int = 200) -> dict:
    """Returns {record_key: {lat, lon, precision, provider}}.

    Records that already carry source coordinates are skipped -- a published
    coordinate always beats a geocoded one.
    """
    cache = Cache(cache_path)
    result, done, fresh = {}, 0, 0

    for rec in records:
        if rec.src_lat is not None and rec.src_lon is not None:
            continue
        q = build_query(rec)
        if not q:
            continue

        hit = cache.get(q)
        if hit is None:
            if limit is not None and fresh >= limit:
                continue
            hit = provider(q, contact=contact)
            cache.put(q, hit)
            fresh += 1

        result[rec.key()] = {
            "lat": hit.lat, "lon": hit.lon,
            "precision": hit.precision, "provider": hit.provider,
        }
        done += 1
        if progress_every and done % progress_every == 0:
            print(f"  geocoded {done} ({fresh} fresh lookups)", flush=True)

    cache.save()
    return result


def pending_count(records, cache_path: str = "work/geocache.json.gz") -> tuple[int, int]:
    """(already cached, still to do) — drives the chunked CI loop."""
    cache = Cache(cache_path)
    need = {build_query(r) for r in records
            if r.src_lat is None and build_query(r)}
    have = sum(1 for q in need if cache.get(q) is not None)
    return have, len(need) - have


def unlocated_report(facilities) -> dict:
    """Counts by country for everything the map cannot honestly place."""
    from collections import Counter
    c = Counter(f.country_iso3 for f in facilities if not f.mappable)
    return {
        "unlocated_total": sum(c.values()),
        "by_country": dict(c.most_common()),
    }
