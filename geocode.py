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

Queries are structured, not free-form. The previous version pasted address,
locality, admin1, postcode and a three-letter country fragment into one string
and sent it to Nominatim: `krithia thessaloniki krithia thessaloniki regional
unit central macedonia region macedonia thrace 57200 gre`. That came back empty
almost every time, and the empty answer was cached. Now the street, the town
and the postcode go into their own parameters and the country goes into the
provider's country filter, which also means a French address can no longer
resolve to somewhere in Ontario.

Providers are pluggable. Nominatim is included because it is free and open, but
its usage policy caps you at one request per second and expects a real contact
address in the User-Agent. Photon serves the same OpenStreetMap data with no
per-second rule, so it carries the bulk. The cache means the work happens once.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from countries import english_name, to_iso2
from schema import MAPPABLE_PRECISION

# Bumped whenever the query shape changes. Cache keys carry it, so entries
# written by an older, worse query are ignored rather than trusted: the 31,000
# addresses cached as unresolvable in September 2026 were cached under v1.
CACHE_VERSION = "v2"

# How many times one address may come back empty before it is left alone.
# Without a ceiling a genuinely unlistable address is retried every run forever
# and the pipeline never reports itself finished.
MAX_TRIES = 3


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

    @property
    def mappable(self) -> bool:
        return self.lat is not None and self.precision in MAPPABLE_PRECISION


# ---------------------------------------------------------------------------
# Address components
# ---------------------------------------------------------------------------

# Debris that rides along in the TRACES exports: an establishment number and an
# approval date pasted onto the end of the region field, contact details pasted
# onto the end of an address.
_ID_DATE_TAIL = re.compile(r";\s*\d[\d\s/.\-]*$")
_CONTACT_TAIL = re.compile(r"\b(tel|fax|e-?mail|phone|mob)\b\s*[:.]?.*$", re.I)
_EMAIL = re.compile(r"\S+@\S+")
_WS = re.compile(r"\s+")


def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = str(s).replace("\t", " ")
    s = _ID_DATE_TAIL.sub("", s)
    s = _EMAIL.sub(" ", s)
    s = _CONTACT_TAIL.sub(" ", s)
    s = s.strip(" ,;-")
    return _WS.sub(" ", s).strip()


def _first_admin(s: str | None) -> str:
    """TRACES prints the whole administrative chain in one field --
    `Creuse,New Aquitaine,Metropolitan France`. The most specific element is
    the one a gazetteer can use; the rest pulls a match towards the wrong end
    of the country."""
    c = _clean(s)
    if not c:
        return ""
    return _clean(c.split(";")[0].split(",")[0])


def parts(rec) -> dict:
    """The pieces of one record's address, each in its own field.

    Nothing here is invented: an absent element stays absent, and a record
    whose country cannot be resolved gets no country filter rather than a
    guessed one.
    """
    iso3 = (getattr(rec, "country_iso3", "") or "").upper() or None
    return {
        "street": _clean(getattr(rec, "address", None)),
        "city": (_clean(getattr(rec, "locality", None))
                 or _first_admin(getattr(rec, "admin1", None))),
        "state": _first_admin(getattr(rec, "admin1", None)),
        "postcode": _clean(getattr(rec, "postcode", None)),
        "iso3": iso3,
        "iso2": to_iso2(iso3),
        "country": english_name(iso3) or "",
    }


def build_query(rec) -> str:
    """Cache key for a record's address.

    Two records with the same address share a key and cost one lookup between
    them. The key carries the cache version, so changing the query shape
    retires the old answers instead of inheriting them.
    """
    p = parts(rec)
    if not (p["street"] or p["city"] or p["postcode"]):
        return ""
    body = "|".join([
        (p["iso2"] or p["iso3"] or "??"),
        p["street"].lower(),
        p["city"].lower(),
        p["postcode"].lower().replace(" ", ""),
    ])
    return f"{CACHE_VERSION}|{body}"


def _has_street(p: dict) -> bool:
    """A street line worth sending as a street: it has to carry something more
    specific than a settlement name, which in practice means a number."""
    return bool(p["street"]) and bool(re.search(r"\d", p["street"]))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class Cache:
    """Gzipped JSON, keyed by the versioned query key.

    Deliberately a single portable file rather than a database. It has to
    survive being committed back to a repository between CI runs, and a few MB
    gzipped does that where a SQLite file does not: the whole point of the
    cache is that hours of geocoding only ever happen once.
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

    def drop_stale(self) -> int:
        """Remove entries written under an older cache version. Their keys can
        never be looked up again, so keeping them only inflates the file."""
        stale = [q for q in self._d if not q.startswith(CACHE_VERSION + "|")]
        for q in stale:
            del self._d[q]
        if stale:
            self._dirty += len(stale)
        return len(stale)

    def raw(self, q: str) -> dict | None:
        return self._d.get(q)

    def get(self, q: str) -> GeoHit | None:
        r = self._d.get(q)
        return GeoHit(r["lat"], r["lon"], r["precision"], r["provider"]) if r else None

    def tries(self, q: str) -> int:
        r = self._d.get(q)
        return int(r.get("tries", 1)) if r else 0

    def put(self, q: str, hit: GeoHit) -> None:
        self._d[q] = {"lat": hit.lat, "lon": hit.lon,
                      "precision": hit.precision, "provider": hit.provider,
                      "tries": self.tries(q) + 1}
        self._dirty += 1
        if self._dirty >= 250:          # flush often; CI jobs get killed
            self.save()

    def outstanding(self, q: str) -> bool:
        """True when this address is still worth a request: never tried, or
        tried and returned nothing, and not yet at the ceiling. A coarse answer
        counts as answered -- retrying it will not produce a street."""
        r = self._d.get(q)
        if r is None:
            return True
        if r.get("precision") in MAPPABLE_PRECISION:
            return False
        if r.get("precision") in ("locality", "admin"):
            return False
        return int(r.get("tries", 1)) < MAX_TRIES

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(self._d, fh, separators=(",", ":"))
        tmp.replace(self.path)
        self._dirty = 0


# ---------------------------------------------------------------------------
# Nominatim
# ---------------------------------------------------------------------------

_ROOFTOP_TYPES = {"building", "house", "yes", "industrial", "commercial",
                  "retail", "warehouse", "amenity", "shop", "office", "craft",
                  "man_made", "farm", "farmyard"}
_ROOFTOP_CLASSES = {"building", "amenity", "shop", "man_made", "office",
                    "craft", "industrial"}


def _nominatim_precision(top: dict) -> str:
    addr = top.get("address", {}) or {}
    cls = top.get("category") or top.get("class")
    atype = top.get("addresstype") or top.get("type")
    if addr.get("house_number") or cls in _ROOFTOP_CLASSES or atype in _ROOFTOP_TYPES:
        return "rooftop"
    if atype == "road" or addr.get("road"):
        return "street"
    if atype in {"city", "town", "village", "hamlet", "suburb", "municipality",
                 "locality", "postcode", "quarter", "neighbourhood"}:
        return "locality"
    return "admin"


def _get_json(url: str, contact: str) -> tuple[object | None, str | None]:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"abattoir-atlas/2.0 ({contact})",
        "Accept": "application/json",
        "Accept-Language": "en",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp), None
    except Exception as exc:            # network, rate limit, malformed
        return None, str(exc)


def nominatim(p: dict, *, contact: str, pace=None,
              base: str = "https://nominatim.openstreetmap.org") -> GeoHit:
    """Structured search, narrowed to the record's country.

    Attempts run from most specific to least and stop at the first answer good
    enough to draw. A town-level answer is returned rather than discarded, but
    it is labelled `locality` and the map will not pin it.
    """
    attempts: list[dict] = []
    if _has_street(p):
        a = {"street": p["street"]}
        if p["city"]:
            a["city"] = p["city"]
        if p["postcode"]:
            a["postalcode"] = p["postcode"]
        attempts.append(a)
        if p["postcode"] and p["city"]:
            attempts.append({"street": p["street"], "postalcode": p["postcode"]})
    if p["city"] or p["postcode"]:
        a = {}
        if p["city"]:
            a["city"] = p["city"]
        if p["postcode"]:
            a["postalcode"] = p["postcode"]
        attempts.append(a)

    best = GeoHit(None, None, "none", "nominatim")
    for a in attempts[:3]:
        q = dict(a, format="jsonv2", limit="1", addressdetails="1")
        if p["iso2"]:
            q["countrycodes"] = p["iso2"].lower()
        if pace:
            pace()
        data, err = _get_json(base + "/search?" + urllib.parse.urlencode(q), contact)
        if err is not None:
            return GeoHit(None, None, "none", "nominatim", None, error=err)
        if not isinstance(data, list) or not data:
            continue
        top = data[0]
        cc = ((top.get("address") or {}).get("country_code") or "").upper()
        if p["iso2"] and cc and cc != p["iso2"]:
            continue                  # answered in the wrong country; not an answer
        hit = GeoHit(float(top["lat"]), float(top["lon"]),
                     _nominatim_precision(top), "nominatim", top)
        if hit.mappable:
            return hit
        if best.lat is None:
            best = hit
    return best


# ---------------------------------------------------------------------------
# Photon
# ---------------------------------------------------------------------------
#
# Komoot's Photon runs on the same OpenStreetMap data Nominatim does, so the
# coordinates are of equal provenance -- but it is built for autocomplete and
# has no one-per-second rule. It has no country filter, so the country is
# checked on the way back out instead.

_PHOTON_ROOFTOP = {"house", "building", "industrial", "commercial", "retail",
                   "amenity", "shop", "office", "craft", "warehouse", "farm"}


def _photon_strings(p: dict) -> list[str]:
    out = []
    tail = ", ".join(x for x in (p["postcode"], p["city"], p["country"]) if x)
    if _has_street(p):
        out.append(", ".join(x for x in (p["street"], tail) if x))
    if p["city"] or p["postcode"]:
        out.append(tail)
    return [q for q in out if q]


def photon(p: dict, *, contact: str, pace=None,
           base: str = "https://photon.komoot.io") -> GeoHit:
    best = GeoHit(None, None, "none", "photon")
    for qs in _photon_strings(p)[:2]:
        if pace:
            pace()
        url = base + "/api?" + urllib.parse.urlencode(
            {"q": qs, "limit": 1, "lang": "en"})
        data, err = _get_json(url, contact)
        if err is not None:
            return GeoHit(None, None, "none", "photon", None, error=err)
        feats = (data or {}).get("features") or []
        if not feats:
            continue
        f = feats[0]
        props = f.get("properties", {}) or {}
        cc = (props.get("countrycode") or "").upper()
        if p["iso2"] and cc and cc != p["iso2"]:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        key, val = props.get("osm_key"), props.get("osm_value")
        if props.get("housenumber") or key in _PHOTON_ROOFTOP or val in _PHOTON_ROOFTOP:
            precision = "rooftop"
        elif props.get("street") or key == "highway":
            precision = "street"
        elif props.get("type") in {"city", "district", "locality"} or key == "place":
            precision = "locality"
        else:
            precision = "admin"
        hit = GeoHit(float(lat), float(lon), precision, "photon", props)
        if hit.mappable:
            return hit
        if best.lat is None:
            best = hit
    return best


# ---------------------------------------------------------------------------
# Provider lanes
# ---------------------------------------------------------------------------

@dataclass
class Lane:
    """One provider plus the rate it may be called at.

    Lanes run concurrently, each self-limited, so the combined throughput is the
    sum of what every provider allows rather than the slowest one. Nothing here
    relaxes an individual provider's policy -- Nominatim still gets one request
    per second, it just is not the only thing running. The pace callback goes
    into the provider so that an attempt ladder costs the lane its interval per
    request rather than per record.
    """
    name: str
    fn: object
    min_interval: float
    _next_at: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
        self._next_at = max(now, self._next_at) + self.min_interval


DEFAULT_LANES = [
    ("photon", photon, 0.25),
    ("photon", photon, 0.25),
    ("photon", photon, 0.25),
    ("photon", photon, 0.25),
    ("nominatim", nominatim, 1.1),
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _query_index(records) -> dict[str, dict]:
    """{cache key: {"parts": ..., "keys": [record keys]}} for everything that
    needs a lookup. Records carrying source coordinates are skipped -- a
    published coordinate always beats a geocoded one."""
    idx: dict[str, dict] = {}
    for rec in records:
        if rec.src_lat is not None and rec.src_lon is not None:
            continue
        q = build_query(rec)
        if not q:
            continue
        entry = idx.setdefault(q, {"parts": parts(rec), "keys": []})
        entry["keys"].append(rec.key())
    return idx


def geocode_records(records, *, contact: str, cache_path: str = "work/geocache.json.gz",
                    provider=None, limit: int | None = None,
                    progress_every: int = 500, lanes=None,
                    fallback: bool = True, max_minutes: float | None = None) -> dict:
    """Returns {record_key: {lat, lon, precision, provider}}.

    Work is distributed over concurrent lanes, each holding its own provider to
    its own rate. A query that comes back empty from a fast lane is retried once
    on Nominatim before being recorded as unresolvable, so speed never costs a
    result: the slow, authoritative provider still sees everything the fast one
    could not place.

    max_minutes stops the run cleanly before a CI job's own timeout kills it.
    A killed job never reaches the step that commits the cache, so every lookup
    it made is thrown away; stopping early and committing what is done keeps
    the next run starting where this one stopped.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cache = Cache(cache_path)
    dropped = cache.drop_stale()
    if dropped:
        print(f"dropped {dropped:,} entries cached under an older query shape; "
              f"those addresses are looked up again", flush=True)

    idx = _query_index(records)
    todo = [q for q in idx if cache.outstanding(q)]
    if limit is not None:
        todo = todo[:limit]

    print(f"{len(idx):,} distinct addresses, {len(idx)-len(todo):,} already answered, "
          f"{len(todo):,} to look up", flush=True)

    if provider is not None:                      # single-provider override
        lane_specs = [("custom", provider, 1.1)]
    else:
        lane_specs = lanes or DEFAULT_LANES
    lane_objs = [Lane(n, f, i) for n, f, i in lane_specs]

    lock = threading.Lock()
    counter = {"done": 0, "hit": 0, "coarse": 0, "retried": 0, "errors": 0,
               "stopped_early": False}
    total = len(todo)
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None

    def work(lane_idx: int):
        lane = lane_objs[lane_idx % len(lane_objs)]
        while True:
            with lock:
                if not todo:
                    return
                if deadline is not None and time.monotonic() > deadline:
                    counter["stopped_early"] = True
                    return
                q = todo.pop()
            p = idx[q]["parts"]
            hit = lane.fn(p, contact=contact, pace=lane.wait)

            # A fast lane drawing a blank is not a verdict. Ask Nominatim before
            # writing "not found" into a cache that later runs will trust.
            if fallback and not hit.mappable and lane.name != "nominatim":
                nom = next((l for l in lane_objs if l.name == "nominatim"), None)
                if nom is not None:
                    retry = nom.fn(p, contact=contact, pace=nom.wait)
                    with lock:
                        counter["retried"] += 1
                    if retry.mappable or (hit.lat is None and retry.lat is not None):
                        hit = retry

            with lock:
                if hit.cacheable:
                    cache.put(q, hit)
                else:
                    counter["errors"] += 1
                counter["done"] += 1
                if hit.mappable:
                    counter["hit"] += 1
                elif hit.lat is not None:
                    counter["coarse"] += 1
                if progress_every and counter["done"] % progress_every == 0:
                    print(f"  {counter['done']:,}/{total:,} looked up, "
                          f"{counter['hit']:,} placed, "
                          f"{counter['coarse']:,} town-level only, "
                          f"{counter['retried']:,} retried, "
                          f"{counter['errors']:,} request failures", flush=True)

    with ThreadPoolExecutor(max_workers=len(lane_objs)) as pool:
        list(pool.map(work, range(len(lane_objs))))

    cache.save()

    if counter["stopped_early"]:
        print(f"\nstopped at the {max_minutes:g}-minute budget with "
              f"{len(todo):,} addresses still queued. Everything looked up is "
              f"in the cache; the next run picks up from there.", flush=True)

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
                  f"query shape rather than the data. Run `python run.py "
                  f"geocode --contact ... --probe 25` to see the queries and "
                  f"what came back.", flush=True)

    return coords_for_records(records, cache_path=cache_path, _cache=cache, _idx=idx)


def coords_for_records(records, cache_path: str = "work/geocache.json.gz",
                       _cache: "Cache | None" = None,
                       _idx: dict | None = None) -> dict:
    """{record_key: {lat, lon, precision, provider}} straight from the cache.

    This is what lets the build job use the coordinates the geocode job found.
    The per-record mapping used to live in work/geo.json, which was written but
    never committed, so every build re-clustered as if nothing had been
    geocoded and the map showed only the sources that publish their own
    coordinates. The cache is the committed artifact; the mapping is derived
    from it here.
    """
    cache = _cache or Cache(cache_path)
    idx = _idx if _idx is not None else _query_index(records)
    out = {}
    for q, entry in idx.items():
        hit = cache.get(q)
        if hit is None or hit.lat is None:
            continue
        for k in entry["keys"]:
            out[k] = {"lat": hit.lat, "lon": hit.lon,
                      "precision": hit.precision, "provider": hit.provider}
    return out


def pending_count(records, cache_path: str = "work/geocache.json.gz") -> tuple[int, int]:
    """(resolved, still outstanding), counted over distinct addresses.

    "Resolved" means a coordinate at street precision or better. The two halves
    of this module used to disagree: the driver treated any cache entry as done
    while this counted a cached miss as outstanding, so the workflow reported
    30,954 addresses left and then looked up none of them, run after run. Both
    now ask Cache.outstanding.
    """
    cache = Cache(cache_path)
    idx = _query_index(records)
    resolved = sum(1 for q in idx
                   if (h := cache.get(q)) is not None
                   and h.precision in MAPPABLE_PRECISION)
    outstanding = sum(1 for q in idx if cache.outstanding(q))
    return resolved, outstanding


def cache_breakdown(records, cache_path: str = "work/geocache.json.gz") -> dict:
    """Counts by outcome over distinct addresses, for `run.py status`."""
    from collections import Counter
    cache = Cache(cache_path)
    idx = _query_index(records)
    c = Counter()
    for q in idx:
        hit = cache.get(q)
        if hit is None:
            c["never tried"] += 1
        elif hit.precision in MAPPABLE_PRECISION:
            c[hit.precision] += 1
        elif hit.precision in ("locality", "admin"):
            c["town-level only"] += 1
        elif cache.tries(q) >= MAX_TRIES:
            c["gave up"] += 1
        else:
            c["empty, will retry"] += 1
    return dict(c)


def purge_misses(cache_path: str = "work/geocache.json.gz") -> tuple[int, int]:
    """Drop cached entries that resolved to nothing, keeping every real hit,
    so the next run looks at them again."""
    cache = Cache(cache_path)
    before = len(cache)
    cache._d = {q: v for q, v in cache._d.items()
                if v.get("precision") in ("rooftop", "street", "locality", "admin")}
    cache.save()
    return before, len(cache)


def probe(records, *, contact: str, n: int = 25,
          cache_path: str = "work/geocache.json.gz") -> None:
    """Look up a small sample and print the query and the answer for each.

    A 31,000-address run takes hours. This takes under a minute, and it is the
    thing to run first after touching the query shape.
    """
    import random
    idx = _query_index(records)
    sample = random.Random(0).sample(sorted(idx), min(n, len(idx)))
    fast, slow = Lane("photon", photon, 0.25), Lane("nominatim", nominatim, 1.1)
    placed = coarse = 0
    for i, q in enumerate(sample, 1):
        p = idx[q]["parts"]
        hit = fast.fn(p, contact=contact, pace=fast.wait)
        if not hit.mappable:
            hit = slow.fn(p, contact=contact, pace=slow.wait)
        placed += bool(hit.mappable)
        coarse += bool(hit.lat is not None and not hit.mappable)
        where = f"{hit.lat:.5f},{hit.lon:.5f}" if hit.lat is not None else "--"
        print(f"{i:>3}. {p['iso3'] or '???'}  {p['street'][:34]:34} | "
              f"{p['city'][:18]:18} | {p['postcode'][:9]:9} -> "
              f"{hit.precision:8} {where:22} {hit.provider}"
              + (f"  ERROR {hit.error}" if hit.error else ""))
    print(f"\n{placed} of {len(sample)} placed at street precision or better, "
          f"{coarse} town-level only, {len(sample)-placed-coarse} nothing.")


def unlocated_report(facilities) -> dict:
    """Counts by country for everything the map cannot honestly place."""
    from collections import Counter
    c = Counter(f.country_iso3 for f in facilities if not f.mappable)
    return {
        "unlocated_total": sum(c.values()),
        "by_country": dict(c.most_common()),
    }
