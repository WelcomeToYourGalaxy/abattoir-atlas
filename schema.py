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
    # TRACES POU-EST-AP covers breeding flocks and production flocks in one
    # approval and does not say which. Inventing farm_eggs or farm_meat for
    # each would be a guess; this records what the register actually states.
    "farm_poultry",
    # TRACES REG-TRANS-AUTH-II: a haulier approved for journeys over eight
    # hours. The listing's address is a company office, not a place animals are
    # kept, and the activity name says so rather than implying a facility.
    "transporter",
    # TRACES BBEEISO-EST: bumble bees reared in environmental isolation, for
    # pollination rather than honey, so farm_honey would misdescribe them.
    "insect_rearing",
    # TRACES germinal-product sections: semen collection centres, embryo
    # collection and production teams, processing and storage. Breeding
    # infrastructure rather than a place animals are killed.
    "germinal_products",
    # TRACES QUR: animals held in isolation before they may move or enter.
    # Distinct enough from an assembly centre to name separately -- the holding
    # is compulsory and the animals are under restriction, not in transit.
    "quarantine",
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

# Only these two precisions are safe to draw as a facility pin. Anything coarser
# is a placeholder that would imply a precision the data does not have.
MAPPABLE_PRECISION = {"rooftop", "street"}

# The wider set dedup.py uses when attaching a coordinate to a facility, as
# opposed to when deciding whether two records are the same site. A town
# centroid is worth carrying and labelling; it is not worth matching on, since
# every plant in one town shares it and a proximity test would merge them all.
# Facility.mappable still gates on MAPPABLE_PRECISION, so a locality coordinate
# reaches the published data without ever being drawn as a pin.
DRAWABLE_PRECISION = {"rooftop", "street", "locality"}


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
    # How exact the source's own position is, where the source's file shows it is
    # not the building: "locality" for a town-centre placement. None means the
    # position is taken as published, as it always was.
    src_precision: str | None = None

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
    def mappable(self) -> bool:
        return (
            self.lat is not None
            and self.lon is not None
            and self.geo_precision in MAPPABLE_PRECISION
        )

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
