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
    error: str | None = None        # transport/HTTP failure, not a real answer

    @property
    def cacheable(self) -> bool:
        """A refused or failed request is not evidence that an address cannot
        be placed. Caching one turns a temporary outage -- or a missing contact
        string -- into a permanent hole that every later run trusts and skips."""
        return self.error is None


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
        return GeoHit(None, None, "none", "nominatim", None, error=str(exc))
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


# --- Photon ---------------------------------------------------------------
#
# Komoot's Photon runs on the same OpenStreetMap data Nominatim does, so the
# coordinates are of equal provenance -- but it is built for autocomplete and
# has no one-per-second rule. It is the single biggest speed-up available
# without paying anyone or lowering the standard of the result.

_PHOTON_ROOFTOP = {"house", "building", "industrial", "commercial", "retail",
                   "amenity", "shop", "office", "craft"}


def photon(query: str, *, contact: str,
           base: str = "https://photon.komoot.io", pause: float = 0.12) -> GeoHit:
    url = base + "/api?" + urllib.parse.urlencode({"q": query, "limit": 1})
    req = urllib.request.Request(url, headers={
        "User-Agent": f"abattoir-atlas/1.0 ({contact})",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:
        return GeoHit(None, None, "none", "photon", None, error=str(exc))
    finally:
        if pause:
            time.sleep(pause)

    feats = data.get("features") or []
    if not feats:
        return GeoHit(None, None, "none", "photon", None)

    f = feats[0]
    lon, lat = f["geometry"]["coordinates"][:2]
    props = f.get("properties", {})
    osm_key, osm_val = props.get("osm_key"), props.get("osm_value")

    if props.get("housenumber") or osm_key in _PHOTON_ROOFTOP or osm_val in _PHOTON_ROOFTOP:
        precision = "rooftop"
    elif props.get("street") or osm_key == "highway":
        precision = "street"
    elif props.get("type") in {"city", "district", "locality"} or osm_key == "place":
        precision = "locality"
    else:
        precision = "admin"

    return GeoHit(float(lat), float(lon), precision, "photon", props)


# --- provider lanes -------------------------------------------------------

@dataclass
class Lane:
    """One provider plus the rate it may be called at.

    Lanes run concurrently, each self-limited, so the combined throughput is the
    sum of what every provider allows rather than the slowest one. Nothing here
    relaxes an individual provider's policy -- Nominatim still gets one request
    per second, it just is not the only thing running.
    """
    name: str
    fn: object
    min_interval: float          # seconds between this lane's requests
    _next_at: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
        self._next_at = max(now, self._next_at) + self.min_interval


DEFAULT_LANES = [
    # Photon first and in quantity: same OSM source, no per-second rule.
    ("photon", photon, 0.25),
    ("photon", photon, 0.25),
    ("photon", photon, 0.25),
    ("photon", photon, 0.25),
    # Nominatim alongside, strictly at its documented one per second.
    ("nominatim", nominatim, 1.1),
]


# --- driver ----------------------------------------------------------------

def geocode_records(records, *, contact: str, cache_path: str = "work/geocache.json.gz",
                    provider=None, limit: int | None = None,
                    progress_every: int = 500, lanes=None,
                    fallback: bool = True) -> dict:
    """Returns {record_key: {lat, lon, precision, provider}}.

    Records that already carry source coordinates are skipped -- a published
    coordinate always beats a geocoded one.

    Work is distributed over concurrent lanes, each holding its own provider to
    its own rate. A query that comes back empty from a fast lane is retried once
    on Nominatim before being recorded as unresolvable, so speed never costs a
    result: the slow, authoritative provider still sees everything the fast one
    could not place.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cache = Cache(cache_path)

    # One lookup per distinct address, not per record. Records sharing an
    # address share the answer.
    queries: dict[str, list] = {}
    for rec in records:
        if rec.src_lat is not None and rec.src_lon is not None:
            continue
        q = build_query(rec)
        if q:
            queries.setdefault(q, []).append(rec.key())

    todo = [q for q in queries if cache.get(q) is None]
    if limit is not None:
        todo = todo[:limit]

    print(f"{len(queries):,} distinct addresses, {len(queries)-len(todo):,} cached, "
          f"{len(todo):,} to look up", flush=True)

    if provider is not None:                      # single-provider override
        lane_specs = [("custom", provider, 1.1)]
    else:
        lane_specs = lanes or DEFAULT_LANES
    lane_objs = [Lane(n, f, i) for n, f, i in lane_specs]

    lock = threading.Lock()
    counter = {"done": 0, "hit": 0, "retried": 0, "errors": 0}

    def work(idx: int):
        lane = lane_objs[idx % len(lane_objs)]
        while True:
            with lock:
                if not todo:
                    return
                q = todo.pop()
            lane.wait()
            hit = lane.fn(q, contact=contact)

            # A fast lane drawing a blank is not a verdict. Ask Nominatim before
            # writing "not found" into a cache that later runs will trust.
            if fallback and hit.precision == "none" and lane.name != "nominatim":
                nom = next((l for l in lane_objs if l.name == "nominatim"), None)
                if nom is not None:
                    nom.wait()
                    retry = nom.fn(q, contact=contact)
                    with lock:
                        counter["retried"] += 1
                    if retry.precision != "none":
                        hit = retry

            with lock:
                if hit.cacheable:
                    cache.put(q, hit)
                else:
                    counter["errors"] += 1
                counter["done"] += 1
                if hit.precision in ("rooftop", "street"):
                    counter["hit"] += 1
                if progress_every and counter["done"] % progress_every == 0:
                    print(f"  {counter['done']:,}/{len(queries):,} looked up, "
                          f"{counter['hit']:,} placed, "
                          f"{counter['retried']:,} retried, "
                          f"{counter['errors']:,} request failures", flush=True)

    with ThreadPoolExecutor(max_workers=len(lane_objs)) as pool:
        list(pool.map(work, range(len(lane_objs))))

    cache.save()

    done, errs, hits = counter["done"], counter["errors"], counter["hit"]
    if done:
        if errs > done * 0.2:
            print(f"\n!! {errs:,} of {done:,} requests FAILED at the transport "
                  f"level. Nothing was cached for those. Check the contact "
                  f"string and whether the providers are reachable before "
                  f"re-running.", flush=True)
        elif hits < done * 0.2:
            print(f"\n!! only {hits:,} of {done:,} addresses resolved to a "
                  f"street or building. That is low enough to suspect the "
                  f"address format rather than the data.", flush=True)

    result = {}
    for q, keys in queries.items():
        hit = cache.get(q)
        if hit is None:
            continue
        for k in keys:
            result[k] = {"lat": hit.lat, "lon": hit.lon,
                         "precision": hit.precision, "provider": hit.provider}
    return result


def purge_misses(cache_path: str = "work/geocache.json.gz") -> tuple[int, int]:
    """Drop cached entries that resolved to nothing, keeping every real hit.

    Use after a run that was misconfigured: the successful lookups stay, the
    poisoned "not found" entries are removed so the next run retries them.
    """
    cache = Cache(cache_path)
    before = len(cache)
    cache._d = {q: v for q, v in cache._d.items()
                if v.get("precision") in ("rooftop", "street", "locality", "admin")}
    cache.save()
    return before, len(cache)


def unlocated_report(facilities) -> dict:
    """Counts by country for everything the map cannot honestly place."""
    from collections import Counter
    c = Counter(f.country_iso3 for f in facilities if not f.mappable)
    return {
        "unlocated_total": sum(c.values()),
        "by_country": dict(c.most_common()),
    }
