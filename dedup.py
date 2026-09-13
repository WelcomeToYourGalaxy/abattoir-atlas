"""
Deduplication.

Three commitments, in order of importance:

1. Nothing is discarded. Every input row ends up inside exactly one facility
   cluster, and the cluster carries all of its members verbatim. A merge is a
   grouping, never a deletion, so any merge can be inspected and undone.

2. The pipeline does not resolve ambiguity on the user's behalf. Pairs that are
   probably the same site but not provably so go to a review queue as pairs,
   and stay unmerged until a human says otherwise.

3. Merge decisions are attributable. Each cluster records which tier joined it,
   so "why are these one pin" has an answer.

Tiers, applied in order, strongest first:

  id      Same establishment number in the same scheme. This is the tier that
          does the heavy lifting, because a plant exporting to the EU, to China
          and selling domestically appears in three registries all quoting the
          same national number. Treated as proof.

  addr    Same country, same postcode, same street number, and name token
          overlap at or above NAME_STRICT. Treated as proof.

  geo     Both records carry rooftop or street coordinates within GEO_METRES
          of each other, and name token overlap at or above NAME_STRICT.
          Treated as proof.

  review  Name overlap at or above NAME_LOOSE with a weaker locational signal.
          Not merged. Written to the review queue.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from itertools import combinations

from normalize import addr_key, norm_name, norm_id, token_set_ratio
from schema import (DRAWABLE_PRECISION, Facility, MAPPABLE_PRECISION,
                    SourceRecord, make_uid)

# Thresholds. Deliberately conservative: an unmerged duplicate is a visible,
# fixable error, while a wrong merge silently destroys a distinct facility.
NAME_STRICT = 0.60
NAME_LOOSE = 0.40
GEO_METRES = 250.0
GEO_METRES_LOOSE = 1500.0


# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, keys):
        self.parent = {k: k for k in keys}
        self.tiers = defaultdict(set)

    def find(self, k):
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a, b, tier):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            self.tiers[ra].add(tier)
            return
        self.parent[rb] = ra
        self.tiers[ra] |= self.tiers.pop(rb, set())
        self.tiers[ra].add(tier)

    def groups(self):
        out = defaultdict(list)
        for k in self.parent:
            out[self.find(k)].append(k)
        return out


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------

def _coords(rec: SourceRecord, geocache: dict, *,
            precise_only: bool = False) -> tuple[float, float, str] | None:
    """Best available coordinate for a record: source-published first, then
    geocoded.

    precise_only exists because the two callers want different things. Drawing
    a facility on a map can honestly use a town-level coordinate, labelled as
    such. Deciding whether two records are the same site cannot: every plant in
    one town shares that town's centroid, so a 250 m proximity test on locality
    coordinates would merge every abattoir in Parma into one.
    """
    if rec.src_lat is not None and rec.src_lon is not None:
        return (rec.src_lat, rec.src_lon, "rooftop")
    hit = geocache.get(rec.key())
    if not hit or hit.get("lat") is None:
        return None
    prec = hit.get("precision")
    allowed = MAPPABLE_PRECISION if precise_only else DRAWABLE_PRECISION
    if prec in allowed:
        return (hit["lat"], hit["lon"], prec)
    return None


def cluster(records: list[SourceRecord], geocache: dict | None = None):
    """Group source records into facilities.

    Returns (facilities, review_pairs). Blocking is by country, which is safe:
    no registry in scope lists a facility under the wrong country, and it keeps
    the pairwise passes tractable at global scale.
    """
    geocache = geocache or {}
    by_key = {r.key(): r for r in records}
    uf = UnionFind(by_key.keys())
    review_pairs: list[dict] = []

    # ---- tier: id -------------------------------------------------------
    # Index every identifier a record carries, including the foreign number
    # that CIFER-style registries quote from the home authority.
    id_index: dict[str, list[str]] = defaultdict(list)
    for k, r in by_key.items():
        for scheme, value in ((r.id_scheme, r.national_id),
                              (r.foreign_id_scheme, r.foreign_id)):
            nid = norm_id(scheme, value)
            if nid:
                id_index[nid].append(k)

    for nid, keys in id_index.items():
        if len(keys) > 1:
            first = keys[0]
            for other in keys[1:]:
                uf.union(first, other, "id")

    # ---- blocking -------------------------------------------------------
    blocks: dict[str, list[str]] = defaultdict(list)
    for k, r in by_key.items():
        blocks[r.country_iso3 or "ZZZ"].append(k)

    for country, keys in blocks.items():
        # ---- tier: addr -------------------------------------------------
        addr_index: dict[str, list[str]] = defaultdict(list)
        for k in keys:
            r = by_key[k]
            ak = addr_key(r.address, r.postcode)
            if ak and "#" in ak and ak.split("#")[1]:
                addr_index[ak].append(k)

        for ak, group in addr_index.items():
            if len(group) < 2:
                continue
            for a, b in combinations(group, 2):
                if uf.find(a) == uf.find(b):
                    continue
                score = token_set_ratio(by_key[a].name, by_key[b].name)
                if score >= NAME_STRICT:
                    uf.union(a, b, "addr")
                elif score >= NAME_LOOSE:
                    review_pairs.append(_pair(by_key[a], by_key[b], score,
                                              "same postcode and street number",
                                              None))

        # ---- tier: geo --------------------------------------------------
        # Coarse grid buckets keep this near-linear. 0.01 degrees of latitude
        # is roughly 1.1 km, comfortably wider than GEO_METRES_LOOSE.
        grid: dict[tuple, list[str]] = defaultdict(list)
        coord_of: dict[str, tuple] = {}
        for k in keys:
            c = _coords(by_key[k], geocache, precise_only=True)
            if c:
                coord_of[k] = c
                grid[(round(c[0], 2), round(c[1], 2))].append(k)

        seen_pairs = set()
        for (gy, gx), _ in list(grid.items()):
            neighbourhood = []
            for dy in (-0.01, 0.0, 0.01):
                for dx in (-0.01, 0.0, 0.01):
                    neighbourhood += grid.get((round(gy + dy, 2), round(gx + dx, 2)), [])
            for a, b in combinations(sorted(set(neighbourhood)), 2):
                if (a, b) in seen_pairs:
                    continue
                seen_pairs.add((a, b))
                if uf.find(a) == uf.find(b):
                    continue
                la, lo, _ = coord_of[a]
                lb, lob, _ = coord_of[b]
                d = haversine_m(la, lo, lb, lob)
                if d > GEO_METRES_LOOSE:
                    continue
                score = token_set_ratio(by_key[a].name, by_key[b].name)
                if d <= GEO_METRES and score >= NAME_STRICT:
                    uf.union(a, b, "geo")
                elif score >= NAME_LOOSE:
                    review_pairs.append(_pair(by_key[a], by_key[b], score,
                                              f"{int(d)} m apart", d))

        # ---- review only: strong name, weak location --------------------
        # Same normalized name inside one country with no usable address or
        # coordinate on either side. Common in third-country lists that give
        # only a town. Never merged: identical company names across multiple
        # plants are the norm in this industry, not the exception.
        name_index: dict[str, list[str]] = defaultdict(list)
        for k in keys:
            r = by_key[k]
            nn = norm_name(r.name)
            if nn and not addr_key(r.address, r.postcode) and k not in coord_of:
                name_index[nn].append(k)
        for nn, group in name_index.items():
            if len(group) < 2 or len(group) > 12:
                continue
            for a, b in combinations(group, 2):
                if uf.find(a) == uf.find(b):
                    continue
                review_pairs.append(_pair(by_key[a], by_key[b], 1.0,
                                          "identical name, no usable location "
                                          "on either record", None))

    # ---- assemble -------------------------------------------------------
    facilities = []
    for root, member_keys in uf.groups().items():
        facilities.append(_build_facility(
            [by_key[k] for k in member_keys],
            sorted(uf.tiers.get(root, set())),
            geocache,
        ))

    review_keys = {p["a_key"] for p in review_pairs} | {p["b_key"] for p in review_pairs}
    for f in facilities:
        if any(m["key"] in review_keys for m in f.members):
            f.review = True

    return facilities, review_pairs


def _pair(a: SourceRecord, b: SourceRecord, score: float, reason: str, metres):
    return {
        "a_key": a.key(), "a_source": a.source_id, "a_name": a.name,
        "a_address": a.address or "", "a_id": a.national_id or "",
        "b_key": b.key(), "b_source": b.source_id, "b_name": b.name,
        "b_address": b.address or "", "b_id": b.national_id or "",
        "country": a.country_iso3,
        "name_score": round(score, 3),
        "metres_apart": "" if metres is None else int(metres),
        "reason": reason,
        "decision": "",   # user fills: merge | distinct | unsure
    }


def _build_facility(members: list[SourceRecord], tiers: list[str], geocache: dict) -> Facility:
    keys = [m.key() for m in members]

    # Display name: the longest published name, on the reasoning that registries
    # truncate rather than embellish. All variants are kept regardless.
    variants = sorted({m.name for m in members if m.name})
    display = max(variants, key=len) if variants else "(unnamed)"

    # Address: prefer a member that has both a street line and a postcode.
    addr_member = next(
        (m for m in members if m.address and m.postcode),
        next((m for m in members if m.address), members[0]),
    )

    # Coordinates: the most precise available among members. Never averaged --
    # a mean of two geocodes is a location no source published.
    # Most precise available across the cluster. A rooftop hit on one member
    # beats a town centroid on another, so a facility is only ever shown as
    # approximate when no member could be placed better than that.
    rank_of = {"rooftop": 0, "street": 1, "locality": 2}
    best = None
    for m in members:
        c = _coords(m, geocache)
        if not c:
            continue
        rank = rank_of.get(c[2], 9)
        if best is None or rank < best[0]:
            src = "source" if m.src_lat is not None else geocache.get(m.key(), {}).get("provider")
            best = (rank, c[0], c[1], c[2], src)

    # Slaughter: true if any source says so; false only if every source that
    # spoke said no; None if none of them spoke.
    votes = [m.slaughter for m in members if m.slaughter is not None]
    slaughter = True if any(votes) else (False if votes else None)

    species = sorted({s for m in members for s in m.species})
    activities = sorted({a for m in members for a in m.activities})

    return Facility(
        uid=make_uid(keys),
        name=display,
        name_variants=variants,
        country_iso3=members[0].country_iso3,
        address=addr_member.address,
        locality=addr_member.locality,
        admin1=addr_member.admin1,
        lat=best[1] if best else None,
        lon=best[2] if best else None,
        geo_precision=best[3] if best else "none",
        geo_source=best[4] if best else None,
        species=species,
        activities=activities,
        slaughter=slaughter,
        size_class=next((m.size_class for m in members if m.size_class), None),
        operator=next((m.operator for m in members if m.operator), None),
        members=[{
            "key": m.key(),
            "source": m.source_id,
            "snapshot": m.source_snapshot,
            "name": m.name,
            "national_id": m.national_id,
            "id_scheme": m.id_scheme,
            "foreign_id": m.foreign_id,
            "address": m.address,
            "locality": m.locality,
            "postcode": m.postcode,
            "species": m.species,
            "activities": m.activities,
            "slaughter": m.slaughter,
        } for m in sorted(members, key=lambda x: x.source_id)],
        match_tiers=tiers,
    )


def write_review_queue(pairs: list[dict], path: str) -> None:
    if not pairs:
        open(path, "w").close()
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(pairs[0].keys()))
        w.writeheader()
        w.writerows(pairs)


def apply_review_decisions(facilities: list[Facility], decisions_csv: str):
    """Re-run hook: reads a reviewed queue and returns forced merge/split pairs
    so a second pass honours them. Kept separate so the automatic tiers stay
    reproducible and the human decisions stay auditable as their own artifact."""
    forced_merge, forced_split = [], []
    try:
        with open(decisions_csv, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                d = (row.get("decision") or "").strip().lower()
                if d == "merge":
                    forced_merge.append((row["a_key"], row["b_key"]))
                elif d == "distinct":
                    forced_split.append((row["a_key"], row["b_key"]))
    except FileNotFoundError:
        pass
    return forced_merge, forced_split
