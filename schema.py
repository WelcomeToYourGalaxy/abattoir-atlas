"""
Canonical record schema and controlled vocabularies for the global abattoir atlas.

Design rule that governs this whole file: a value only ever enters a field because
a source published it. Nothing is inferred, widened, or filled in. Where a source
is silent, the field is None and stays None. That is why `slaughter` is a
three-state field (True / False / None) rather than a boolean -- "this registry
did not say" is a different fact from "this registry said no".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

# Species groups. Registries use wildly different granularity -- FSIS enumerates
# 28 livestock classes, EU Annex III groups them into sections. We keep the
# coarse group AND the source's own wording in `members[].raw`.
SPECIES = {
    "bovine",       # cattle, buffalo, bison, yak
    "porcine",      # swine incl. feral
    "ovine",        # sheep, lamb
    "caprine",      # goats
    "equine",       # horses, donkeys
    "cervid",       # deer, elk, reindeer, antelope
    "lagomorph",    # rabbit, hare
    "mustelid",     # mink, ferret
    "canine",       # dogs
    "camelid",      # alpaca, llama
    "fish",
    "crustacean",
    "reptile",
    "insect",       # bees
    "poultry",      # all birds incl. ratites
    "farmed_game",  # Annex III Section III
    "wild_game",    # Annex III Section IV
    "other",
}

# What the facility does. `slaughter` is the one that matters for this atlas;
# the rest are carried so the user can decide what counts, rather than the
# pipeline deciding for them.
ACTIVITIES = {
    "slaughter",
    # Farm Transparency Project adds the tiers before and around killing:
    # where animals are raised, held, traded and used. Carried as activities so
    # the map can separate "killed here" from "held here" without dropping
    # anything.
    "farm_meat", "farm_dairy", "farm_eggs", "farm_wool", "farm_skins",
    "farm_honey", "hatchery", "saleyard", "live_market", "holding_yard",
    "experimentation", "zoo", "wildlife", "racing", "rodeo", "entertainment",
    "pet_breeder", "pet_shop", "agricultural_show", "aquaculture",
    "cutting",          # EU "CP" cutting plant
    "processing",       # EU "PP"
    "minced_meat",
    "meat_preparations",
    "game_handling",    # EU "GHE"
    "cold_store",       # EU "CS"
    "rendering",
    "casings",
    "egg_products",
    "unknown",
}

GEO_PRECISION = {
    "rooftop",    # geocoder returned a building / address point
    "street",     # interpolated along a street segment
    "locality",   # town / city centroid -- NOT a facility location
    "admin",      # region or country centroid -- effectively unlocated
    "none",       # no coordinate at all
}

# Precise enough to draw as a facility pin: this is where the site is.
MAPPABLE_PRECISION = {"rooftop", "street"}

# Town-level only. The register named a locality and no usable street, so the
# coordinate is the town, not the plant. Drawn -- because a facility in the
# right town is worth more than a facility nowhere -- but drawn differently,
# and labelled as such wherever it appears. The map must never imply a
# precision the source did not give.
APPROX_PRECISION = {"locality"}

# Coarser than a town (region or country centroid) stays off the map entirely.
# A country centroid is not a location, it is an average.
DRAWABLE_PRECISION = MAPPABLE_PRECISION | APPROX_PRECISION


# ---------------------------------------------------------------------------
# Source record: one row, as one registry published it
# ---------------------------------------------------------------------------

@dataclass
class SourceRecord:
    source_id: str                    # key into sources.yml
    source_snapshot: str              # ISO date the file was downloaded
    source_row_id: str                # stable row identity within that snapshot

    name: str
    country_iso3: str

    # The establishment number as printed by the issuing authority, plus which
    # scheme it belongs to. This pair is the join key that does most of the
    # deduplication work -- see dedup.py, tier "id".
    national_id: str | None = None
    id_scheme: str | None = None      # "US-FSIS", "EU-APPROVAL", "BR-SIF", ...

    # Some registries (notably CIFER) carry BOTH their own number and the
    # home-country number. The second one is what lets a Chinese registration
    # collapse onto a USDA or DAFF record.
    foreign_id: str | None = None
    foreign_id_scheme: str | None = None

    address: str | None = None
    locality: str | None = None
    admin1: str | None = None
    postcode: str | None = None

    species: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    slaughter: bool | None = None     # tri-state, see module docstring

    size_class: str | None = None
    operator: str | None = None

    # Coordinates as published by the source, if any. Distinct from geocoded
    # coordinates so the two never get confused downstream.
    src_lat: float | None = None
    src_lon: float | None = None

    raw: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.source_id}:{self.source_row_id}"


# ---------------------------------------------------------------------------
# Facility: a cluster of one or more SourceRecords believed to be one site
# ---------------------------------------------------------------------------

@dataclass
class Facility:
    uid: str
    name: str                          # display name, chosen by dedup.py
    name_variants: list[str]
    country_iso3: str

    address: str | None = None
    locality: str | None = None
    admin1: str | None = None

    lat: float | None = None
    lon: float | None = None
    geo_precision: str = "none"
    geo_source: str | None = None

    species: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    slaughter: bool | None = None

    size_class: str | None = None
    operator: str | None = None

    members: list[dict] = field(default_factory=list)
    match_tiers: list[str] = field(default_factory=list)
    review: bool = False               # flagged for a human, not auto-resolved
    review_notes: list[str] = field(default_factory=list)

    @property
    def precise(self) -> bool:
        """Located to a street or a building."""
        return (self.lat is not None and self.lon is not None
                and self.geo_precision in MAPPABLE_PRECISION)

    @property
    def approximate(self) -> bool:
        """Located to a town only -- the right settlement, not the right site."""
        return (self.lat is not None and self.lon is not None
                and self.geo_precision in APPROX_PRECISION)

    @property
    def mappable(self) -> bool:
        return self.precise or self.approximate

    def to_dict(self) -> dict:
        return asdict(self)


def make_uid(member_keys: list[str]) -> str:
    """Stable across runs as long as the same rows cluster together.

    Deliberately derived from member identity rather than from a counter, so
    that re-running the pipeline after a registry update keeps the uid of every
    facility whose membership did not change.
    """
    joined = "|".join(sorted(member_keys))
    return "f" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
