"""
Per-source parsers.

Every parser resolves its columns by looking for candidate header names rather
than by fixed index, and raises with the headers it actually found when a
required column is missing. Registries rename columns without notice; a loud
failure on the next run is better than a silent column shift that quietly
mislabels ten thousand plants.

Parsers never infer. If a source does not say whether a plant slaughters, the
record carries slaughter=None and the map shows it as unstated.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path

from countries import country_at, to_iso3
from schema import SPECIES, SourceRecord
from traces_paste import clean_region, recover_species


class HeaderError(RuntimeError):
    pass


def _resolve_exact(headers: list[str], *candidates: str) -> str | None:
    """Exact header match only. Use where a substring match would be dangerous.

    FSIS is the cautionary case: asking for "livestock_slaughter" by substring
    matches `other_voluntary_livestock_slaughter`, a rare category, and the
    result looks like a working column while under-reporting slaughter by two
    thirds.
    """
    norm = {re.sub(r"[^a-z0-9]", "", h.lower()): h for h in headers}
    for cand in candidates:
        c = re.sub(r"[^a-z0-9]", "", cand.lower())
        if c in norm:
            return norm[c]
    return None


def _resolve(headers: list[str], *candidates: str, required: bool = True) -> str | None:
    """Find a header by normalized candidate name or substring."""
    norm = {re.sub(r"[^a-z0-9]", "", h.lower()): h for h in headers}
    for cand in candidates:
        c = re.sub(r"[^a-z0-9]", "", cand.lower())
        if c in norm:
            return norm[c]
    for cand in candidates:
        c = re.sub(r"[^a-z0-9]", "", cand.lower())
        for k, orig in norm.items():
            if c and c in k:
                return orig
    if required:
        raise HeaderError(
            f"none of {candidates} found. Headers present: {sorted(headers)}"
        )
    return None


def _f(row, col):
    if not col:
        return None
    v = (row.get(col) or "").strip()
    return v or None


def _truthy(v) -> bool | None:
    """Tri-state read of a flag column. Blank means the source was silent."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s == "":
        return None
    if s in {"y", "yes", "true", "1", "x", "t"}:
        return True
    if s in {"n", "no", "false", "0", "f"}:
        return False
    return True  # any other non-empty value is treated as a positive listing


# ---------------------------------------------------------------------------
# USDA FSIS
# ---------------------------------------------------------------------------

# FSIS demographic columns naming livestock classes. Mapped to species groups.
_FSIS_SPECIES = {
    "bovine": ["beef", "cow", "steer", "heifer", "bull", "dairy", "veal", "calf",
               "bison", "buffalo", "yak", "cattalo"],
    "porcine": ["swine", "pork", "sow", "boar", "roaster swine", "feral swine"],
    "ovine": ["sheep", "lamb"],
    "caprine": ["goat"],
    "cervid": ["deer", "reindeer", "antelope", "elk"],
    "lagomorph": ["rabbit"],
    "poultry": ["chicken", "fowl", "capon", "turkey", "duck", "goose", "pheasant",
                "quail", "ostrich", "emu", "rhea", "guinea", "squab", "poultry",
                "ratite"],
}


def parse_fsis(directory_csv: Path, demographic_csv: Path | None,
               snapshot: str | None = None) -> list[SourceRecord]:
    """FSIS MPI Directory, optionally enriched with the demographic file.

    The two files join on establishment_number. The directory carries address
    and identity; the demographic file carries the slaughter and species flags,
    which is the part this atlas needs. Run without the demographic file and
    every record comes back with slaughter=None, which is correct rather than
    convenient.
    """
    snapshot = snapshot or date.today().isoformat()

    demo: dict[str, dict] = {}
    demo_headers: list[str] = []
    if demographic_csv and Path(demographic_csv).exists():
        with open(demographic_csv, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            demo_headers = rd.fieldnames or []
            dnum = _resolve(demo_headers, "establishment_number", "establishmentnumber", "estnumber")
            for row in rd:
                key = (row.get(dnum) or "").strip().upper()
                if key:
                    demo[key] = row

    if demographic_csv and Path(demographic_csv).exists():
        # Print what the demographic file actually offers. The slaughter and
        # species flags all come from here, so if the join or the column
        # detection is off, the atlas silently under-reports slaughter and
        # nothing else tells you.
        print(f"  demographic file: {len(demo):,} rows, columns: "
              f"{sorted(demo_headers)}")
    else:
        print("  no demographic file — every record will carry slaughter=None")

    out = []
    with open(directory_csv, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        h = rd.fieldnames or []
        c_num = _resolve(h, "establishment_number", "establishmentnumber", "estnumber")
        c_name = _resolve(h, "establishment_name", "company_name", "companyname", "estabname")
        c_street = _resolve(h, "street", "address", "physicaladdress", required=False)
        c_city = _resolve(h, "city", required=False)
        c_state = _resolve(h, "state", required=False)
        c_zip = _resolve(h, "zip", "zipcode", "postalcode", required=False)
        c_lat = _resolve(h, "latitude", "lat", required=False)
        c_lon = _resolve(h, "longitude", "lon", "lng", required=False)
        c_size = _resolve(h, "size", "haccpsize", "establishmentsize", required=False)
        c_act = _resolve(h, "activities", "inspectionactivities", required=False)
        c_dba = _resolve(h, "dbas", "dba", required=False)

        for row in rd:
            num = (_f(row, c_num) or "").upper()
            if not num:
                continue
            d = demo.get(num, {})

            species, activities, slaughter = set(), set(), None
            if d:
                # Exact columns only. FSIS publishes a top-level `slaughter`
                # flag plus `meat_slaughter` and `poultry_slaughter`; a
                # substring search finds the wrong ones.
                votes = []
                for col in ("slaughter", "meat_slaughter", "poultry_slaughter"):
                    c = _resolve_exact(demo_headers, col)
                    if c:
                        v = _truthy(d.get(c))
                        if v is not None:
                            votes.append(v)
                slaughter = True if any(votes) else (False if votes else None)
                if slaughter:
                    activities.add("slaughter")

                # Species from whichever class columns are flagged.
                for col, val in d.items():
                    if _truthy(val) is not True:
                        continue
                    cl = re.sub(r"[^a-z ]", " ", col.lower())
                    for group, words in _FSIS_SPECIES.items():
                        if any(w in cl for w in words):
                            species.add(group)
                for col_name, act in (("raw_intact", "cutting"),
                                      ("raw_non_intact", "processing"),
                                      ("rte", "processing"),
                                      ("nrte", "processing"),
                                      ("egg_products", "egg_products")):
                    cc = _resolve(demo_headers, col_name, required=False)
                    if cc and _truthy(d.get(cc)) is True:
                        activities.add(act)

            street = _f(row, c_street)
            city = _f(row, c_city)
            state = _f(row, c_state)
            zipc = _f(row, c_zip)
            addr = ", ".join(x for x in (street, city, state) if x)

            lat = lon = None
            try:
                if _f(row, c_lat) and _f(row, c_lon):
                    lat, lon = float(row[c_lat]), float(row[c_lon])
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                        lat = lon = None
            except (TypeError, ValueError):
                lat = lon = None

            out.append(SourceRecord(
                source_id="us_fsis_mpi",
                source_snapshot=snapshot,
                source_row_id=num,
                name=_f(row, c_name) or num,
                country_iso3="USA",
                national_id=num,
                id_scheme="US-FSIS",
                address=addr or None,
                locality=city,
                admin1=state,
                postcode=zipc,
                species=sorted(species),
                activities=sorted(activities) or ["unknown"],
                slaughter=slaughter,
                size_class=(_f(row, c_size)
                            or (d.get(_resolve_exact(demo_headers,
                                                     "slaughter_volume_category") or "")
                                or None)),
                operator=_f(row, c_dba),
                src_lat=lat, src_lon=lon,
                raw={k: v for k, v in row.items() if v} | {"_demographic": bool(d)},
            ))

    joined = sum(1 for r in out if r.raw.get("_demographic"))
    print(f"  joined to demographic data: {joined:,} of {len(out):,}")
    return out


# ---------------------------------------------------------------------------
# EU: third-country lists and member-state lists
# ---------------------------------------------------------------------------

# Annex III to Reg. (EC) 853/2004. Section determines species; the activity
# code determines whether the plant actually kills.
_EU_SECTION_SPECIES = {
    # Annex III section numbers, and the TRACES commodity codes that stand in
    # for them in the establishment directory.
    "I": ["bovine", "porcine", "ovine", "caprine", "equine"],
    "II": ["poultry", "lagomorph"],
    "III": ["farmed_game"],
    "IV": ["wild_game"],
    "RM": ["bovine", "porcine", "ovine", "caprine", "equine"],
    "PM": ["poultry", "lagomorph"],
    "GM": ["farmed_game"],
    "WM": ["wild_game"],
}

_EU_ACTIVITY = {
    "SH": ("slaughter", True),
    "CP": ("cutting", False),
    "GHE": ("game_handling", False),
    "PP": ("processing", False),
    "MM": ("minced_meat", False),
    "MP": ("meat_preparations", False),
    "MSM": ("processing", False),
    "CS": ("cold_store", False),
}


def parse_eu_list(path: Path, *, source_id: str, id_scheme: str,
                  country_iso3: str | None = None,
                  snapshot: str | None = None) -> list[SourceRecord]:
    """EU approved-establishment list, third-country or member-state.

    Layouts differ by publishing authority, so columns are resolved by name.
    Convert XLSX to CSV first (run.py does this) so there is one code path.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        h = rd.fieldnames or []
        c_num = _resolve(h, "approvalnumber", "approval_number", "regnumber",
                         "establishmentnumber", "number", "nr")
        c_name = _resolve(h, "name", "establishmentname", "nameofestablishment")
        c_addr = _resolve(h, "address", "street", required=False)
        c_city = _resolve(h, "city", "town", "locality", required=False)
        c_region = _resolve(h, "region", "province", "admin", required=False)
        c_pc = _resolve(h, "postcode", "postalcode", "zip", required=False)
        c_country = _resolve(h, "country", "countrycode", required=False)
        c_section = _resolve(h, "section", required=False)
        c_act = _resolve(h, "activity", "activities", "activitycode", required=False)
        c_remark = _resolve(h, "remark", "remarks", "note", required=False)
        c_slaughter = _resolve(h, "slaughter", required=False)
        c_species = _resolve(h, "species", required=False)
        c_group = _resolve(h, "group", required=False)

        for i, row in enumerate(rd):
            num = _f(row, c_num)
            name = _f(row, c_name)
            if not (num or name):
                continue

            section = (_f(row, c_section) or "").strip().upper()
            if section not in _EU_SECTION_SPECIES:
                section = re.sub(r"[^IVX]", "", section)

            # Per-row species when the source gives it (TRACES lists them in
            # Remarks), section-level only as a fallback. The difference is
            # "this plant kills pigs" versus "this plant kills some ungulate".
            # The region column in the harvested CSVs can carry an approval
            # number, a date, or a species clause the paste conversion could not
            # classify. Clean it here as well as at conversion time, so files
            # already in raw/ get the benefit without being re-harvested.
            region = clean_region(_f(row, c_region))

            row_species = (_f(row, c_species) or "").split()
            species = ([s for s in row_species if s in SPECIES]
                       or recover_species(_f(row, c_region))
                       or list(_EU_SECTION_SPECIES.get(section, [])))

            acts, slaughter = set(), None
            raw_act = (_f(row, c_act) or "").upper()
            codes = re.findall(r"\b(SH|CP|GHE|PP|MSM|MM|MP|CS)\b", raw_act)
            for code in codes:
                act, kills = _EU_ACTIVITY[code]
                acts.add(act)
                if kills:
                    slaughter = True
            if codes and slaughter is None:
                slaughter = False   # activity codes present and none of them kill
            flag = _truthy(_f(row, c_slaughter)) if c_slaughter else None
            if flag is not None:
                slaughter = flag

            # Two registers share this folder. Splitting them by source id is
            # what lets the map filter "EU member states" apart from "non-EU
            # countries approved to export into the EU" -- the country name
            # cannot do it, since Northern Ireland sits in the EU group.
            grp = (_f(row, c_group) or "").strip().lower()
            sid = source_id
            if grp == "eu-efta":
                sid = "eu_traces_member_state"
            elif grp == "third-country":
                sid = "eu_traces_third_country"

            out.append(SourceRecord(
                source_id=sid,
                source_snapshot=snapshot,
                source_row_id=num or f"row{i}",
                name=name or num,
                country_iso3=(to_iso3(country_iso3) or to_iso3(_f(row, c_country))
                              or to_iso3(path.stem.replace("-", " ")) or ""),
                national_id=num,
                id_scheme=id_scheme,
                address=_f(row, c_addr),
                locality=_f(row, c_city),
                admin1=region or None,
                postcode=_f(row, c_pc),
                species=species,
                activities=sorted(acts) or ["unknown"],
                slaughter=slaughter,
                raw={k: v for k, v in row.items() if v} | {"section": section,
                                                           "remark": _f(row, c_remark)},
            ))
    return out


# ---------------------------------------------------------------------------
# TRACES animal health register
# ---------------------------------------------------------------------------
#
# A second EU register, separate from the food-hygiene one above. That one
# lists plants approved to handle meat; this lists premises approved to hold
# live animals. No section in it has a slaughter concept, so nothing here is
# ever flagged as killing -- these are the places animals are held and raised
# before they reach one that does.

_HEALTH_SECTION_ACTIVITY = {
    "ASC-UNG": "holding_yard",      # assembly centre, ungulates
    "ASC-POU": "holding_yard",      # assembly centre, poultry
    "ASC-DCF": "holding_yard",      # assembly centre, dogs, cats, ferrets
    "POU-EST-AP": "farm_poultry",   # breeding or productive poultry
    "HATCH-AP": "hatchery",
    "AQUA-EST-AP": "aquaculture",
    "AQUA-EST-AP-GR": "aquaculture",
    # Approved to take in and kill farmed aquatic animals from waters under a
    # disease restriction. The only section in this register where killing is
    # part of the approval, and the rows carry the same generic "Aquaculture
    # establishment" activity text as an ordinary farm -- so the section code,
    # not the row text, is what tells them apart.
    "AQUA-EST-DISEASE": "slaughter",
    "CONF": "zoo",                  # confined establishments, zoos, collections
    "QUR": "quarantine",
    "COP": "holding_yard",          # control post, transport rest stop
    "SEM-COL": "germinal_products",       # semen collection centres
    "EMB-COL": "germinal_products",       # embryo collection teams
    "EMB-PRO": "germinal_products",       # in vitro embryo production teams
    "GERM-PRO": "germinal_products",      # germinal product processing
    "GERM-STO": "germinal_products",      # germinal product storage
    "GERM-CONF": "germinal_products",     # germinal products, confined animals
    "BBEEISO-EST": "insect_rearing",      # isolated bumble bee production
    "REG-TRANS-AUTH-II": "transporter",   # hauliers, journeys over 8 hours
    "REG-TRANS-AUTH-I": "transporter",    # hauliers, journeys up to 8 hours
    "DCF-SHEL": "pet_shop",         # animal shelters
    "BIRD-EST": "pet_breeder",      # captive birds
}
# Sections whose approval includes killing animals on site.
_HEALTH_SLAUGHTER_SECTIONS = {"AQUA-EST-DISEASE"}

_HEALTH_ACTIVITY_TEXT = {
    "assembly center": "holding_yard",
    "assembly centre": "holding_yard",
    "poultry establishment": "farm_poultry",
    "hatchery": "hatchery",
    "quarantine": "quarantine",
    "quarantine establishment": "quarantine",
    "control post": "holding_yard",
    "type ii authorised transporters": "transporter",
    "type i authorised transporters": "transporter",
}


# Donor species as the germinal sections spell them.
_HEALTH_SPECIES_WORDS = {"bovine": "bovine", "equine": "equine",
                         "ovine": "ovine", "caprine": "caprine",
                         "porcine": "porcine"}


def _species_from_text(text: str | None) -> list:
    low = (text or "").lower()
    return [v for k, v in _HEALTH_SPECIES_WORDS.items()
            if re.search(rf"\b{k}\b", low) and v in SPECIES]


def parse_eu_health(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """TRACES animal-health establishment listing, converted by
    traces_health_paste.py.

    The section code decides what a premises is, with the row's activity text
    as the fallback. The section comes from the block header and cannot be
    shifted by a lost pipe, and it is the more specific of the two: every
    aquaculture row says "Aquaculture establishment" whether it is an ordinary
    farm or one approved to kill stock out of a disease-restricted zone.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            approval = (row.get("approval_number") or "").strip()
            name = (row.get("name") or "").strip()
            if not name and not approval:
                continue
            section = (row.get("section") or "").strip().upper()
            act_text = (row.get("activity") or "").split("|")[0].strip().lower()
            activity = (_HEALTH_SECTION_ACTIVITY.get(section)
                        or _HEALTH_ACTIVITY_TEXT.get(act_text)
                        or "unknown")
            species = [sp for sp in (row.get("species") or "").split()
                       if sp in SPECIES]
            # The germinal sections put the donor species in the activity text
            # -- "Bovine collection team", "Ovine/caprine production team" --
            # rather than in the remarks column the other sections use.
            if not species:
                species = _species_from_text(row.get("activity"))
            out.append(SourceRecord(
                source_id="eu_traces_animal_health",
                source_snapshot=snapshot,
                source_row_id=approval or f"row{i}",
                name=name or approval,
                country_iso3=to_iso3(row.get("country")) or "",
                national_id=approval or None,
                id_scheme="TRACES-AH",
                address=(row.get("address") or "").strip() or None,
                locality=(row.get("city") or "").strip() or None,
                admin1=clean_region(row.get("region")) or None,
                postcode=(row.get("postcode") or "").strip() or None,
                species=species,
                activities=[activity],
                # Not "unstated": the register lists what each premises is
                # approved for. Killing is among the options in exactly one
                # section, so one section is flagged and the rest are denied.
                slaughter=(section in _HEALTH_SLAUGHTER_SECTIONS),
                raw={k: v for k, v in row.items() if v},
            ))
    return out


# ---------------------------------------------------------------------------
# EU Industrial Emissions Directive installations
# ---------------------------------------------------------------------------
#
# The EU Registry's installation table, filtered to the IED Annex I activities
# that involve animals. Every row carries a coordinate the operator reported to
# a regulator, which makes this the only European source in the pipeline that
# does not need geocoding -- and a way to check the geocoder's work against
# something authoritative.
#
# Two things it is not. It is a permit register, so nothing below the IED
# capacity threshold appears at all: a slaughterhouse under 50 tonnes of
# carcass a day is invisible here however many animals it kills. And an
# installation is a permitted unit rather than a company, so one site can hold
# several.
#
# 6.6 is the find. Intensive rearing above the thresholds -- 40,000 poultry
# places, 2,000 production pigs, 750 sows -- is 26,559 installations with
# coordinates, an order of magnitude more than the abattoirs.

_IED_ACTIVITY = {
    "6.4(a)": ("slaughter", [], True),       # slaughterhouses >50 t carcass/day
    "6.5":    ("rendering", [], None),       # disposal or recycling of carcases
    "6.6(a)": ("farm_poultry", ["poultry"], None),
    "6.6(b)": ("farm_meat", ["porcine"], None),   # production pigs over 30 kg
    "6.6(c)": ("farm_meat", ["porcine"], None),   # sows
    "6.4(b)(i)": ("processing", [], None),
    "6.4(b)(ii)": ("processing", [], None),
    "6.4(b)(iii)": ("processing", [], None),
    "6.4(c)": ("processing", [], None),      # milk only
    "6.3":    ("processing", [], None),      # tanning of hides and skins
}


def parse_eu_ied(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """EU Registry IED installations, animal activities only.

    Status is carried through rather than filtered on. A disused installation
    is still a place that operated, and which plants to count is not the
    parser's call -- installationStatus rides along in raw.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            code = (row.get("IEDAnnexIMainActivity") or "").strip()
            mapped = _IED_ACTIVITY.get(code)
            if mapped is None:
                continue
            activity, species, kills = mapped

            try:
                lat = float(row["Latitude"])
                lon = float(row["Longitude"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    lat = lon = None
            except (TypeError, ValueError, KeyError):
                lat = lon = None

            name = (row.get("installationName") or "").strip()
            iid = (row.get("InstallationInspireId") or "").strip()
            if not name and not iid:
                continue

            out.append(SourceRecord(
                source_id="eu_industrial_emissions",
                source_snapshot=snapshot,
                source_row_id=iid or f"row{i}",
                name=name or iid,
                country_iso3=(to_iso3(row.get("CountryName"))
                              or country_at(lat, lon) or ""),
                national_id=iid or None,
                id_scheme="EU-INSPIRE",
                locality=(row.get("City_of_Facility") or "").strip() or None,
                species=[sp for sp in species if sp in SPECIES],
                activities=[activity],
                slaughter=kills,
                src_lat=lat, src_lon=lon,
                raw={k: v for k, v in row.items() if v},
            ))
    return out


# ---------------------------------------------------------------------------
# Canada CFIA
# ---------------------------------------------------------------------------
#
# The federally registered meat establishment list. Its ten code columns are
# not ten kinds of code: each column is one facility type from the search
# form's Facility Type panel, in the order that panel lists them, and the
# letters inside a column are that type's sub-options. So CODES_31 holding "fx"
# means boning and cutting of poultry meat and red meat, not a code "fx".
#
# Ritual slaughter and Trichina treatment are absent from the numbered columns
# because the export carries them separately -- TRICHINA1 is its own field.
#
# The column-to-type mapping is read off the search form, not from CFIA's
# function-code document, which is a separate download. It is consistent with
# the counts (97 establishments slaughter, 607 do other processing, 238 are
# storage only) but it is an inference, so the raw codes are kept on every
# record and _CA_UNMAPPED reports any letter the species table does not know.

_CA_COLUMNS = [
    ("CODES_11",  "slaughter",   True),   # Slaughter
    ("CODES_21",  "processing",  False),  # Canning
    ("CODES_31",  "cutting",     False),  # Boning and cutting
    ("CODE_41",   "rendering",   False),  # Edible rendering
    ("CODE_51",   "casings",     False),  # Casing preparation
    ("CODES_61",  "processing",  False),  # Other processing
    ("CODE_71",   "processing",  False),  # Packaging, labelling and storing
    ("CODE_81",   "rendering",   False),  # Inedible rendering
    ("CODES_91",  "cold_store",  False),  # Detained / imported product inspection
    ("CODES_101", "cold_store",  False),  # Storage only: A cold, B dry
]

# Slaughter species, in the order the search form lists them. Letters beyond
# this are left unmapped rather than guessed at.
_CA_SLAUGHTER_SPECIES = {
    "a": "bovine",      # cattle
    "b": "bovine",      # calves
    "c": "ovine",       # sheep, lambs and goats -- the form groups them
    "d": "porcine",     # swine
    "e": "equine",      # horses
    "f": "poultry",
    "g": "lagomorph",   # rabbits
}


def parse_ca_cfia(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """Canada's federally registered meat establishments.

    Federal registration only. Provincially inspected plants, which serve the
    domestic market in most provinces, are licensed separately and are absent.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    unmapped: set = set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            est = (row.get("EST_NUM1") or "").strip()
            name = (row.get("NAME1") or "").strip()
            if not name:
                continue

            acts, species = set(), set()
            for col, activity, is_slaughter in _CA_COLUMNS:
                val = (row.get(col) or "").strip()
                if not val:
                    continue
                acts.add(activity)
                if is_slaughter:
                    for ch in re.findall(r"[A-Za-z]", val):
                        sp = _CA_SLAUGHTER_SPECIES.get(ch.lower())
                        if sp:
                            species.add(sp)
                        else:
                            unmapped.add(ch.lower())

            street = ", ".join(x for x in ((row.get("LOC_ADD11") or "").strip(),
                                           (row.get("LOC_ADD21") or "").strip(),
                                           (row.get("LOC_ADD31") or "").strip()) if x)
            out.append(SourceRecord(
                source_id="ca_cfia",
                source_snapshot=snapshot,
                source_row_id=est or f"row{i}",
                name=name,
                country_iso3="CAN",
                national_id=est or None,
                id_scheme="CA-EST",
                address=street or None,
                locality=(row.get("LOC_CITY1") or "").strip() or None,
                admin1=(row.get("LOC_PROV1") or "").strip() or None,
                postcode=(row.get("LOC_PC1") or "").strip() or None,
                species=sorted(sp for sp in species if sp in SPECIES),
                activities=sorted(acts) or ["unknown"],
                slaughter=True if "slaughter" in acts else None,
                operator=(row.get("ADB_NAME1") or "").strip() or None,
                raw={k: v for k, v in row.items() if v},
            ))
    if unmapped:
        print(f"  ca_cfia: slaughter letters not in the species table: "
              f"{', '.join(sorted(unmapped))} -- check them against CFIA's "
              f"function-code document", flush=True)
    return out


# ---------------------------------------------------------------------------
# New Zealand MPI
# ---------------------------------------------------------------------------
#
# The Animal Products Act register of Risk Management Programmes, exported from
# MPI as a Risk Measure Data Extract. Every row carries a physical address and a
# town, which is rare in this pipeline, and the Primary and Secondary Process
# columns name what the premises does in MPI's own vocabulary.
#
# "Slaughter" and "Dressing" are the two words that mean an animal is killed
# here. Dressing without slaughter is a plant that takes carcasses in, but MPI
# lists the pair together often enough that both are checked and only Slaughter
# sets the flag.

_NZ_PROCESS = [
    ("slaughter", "slaughter"),
    ("dual operator butcher", "cutting"),
    ("boning/cutting", "cutting"),
    ("dressing", "cutting"),
    ("refrigeration", "cold_store"),
    ("rendering", "rendering"),
    ("chicken producer", "farm_meat"),
    ("farm dairy", "farm_dairy"),
    ("candling", "egg_products"),
    ("grading", "egg_products"),
    ("filleting", "processing"),
    ("gutting", "processing"),
    ("shucking", "processing"),
    ("size reduction", "processing"),
    ("thermal", "processing"),
    ("smoking", "processing"),
    ("salting/curing/brining", "processing"),
    ("formulation", "processing"),
    ("packing", "processing"),
]
_NZ_PROGRAMME = [
    ("micro abattoir", "slaughter"),
    ("dual operator butcher", "cutting"),
    ("stores", "cold_store"),
    ("transport and storage", "cold_store"),
    ("bee products", "farm_honey"),
    ("farm dairies", "farm_dairy"),
    ("chicken producers", "farm_meat"),
    ("eggs", "egg_products"),
]
_NZ_SPECIES = [
    (r"\bcattle|\bbovine|\bbobby calf", "bovine"),
    (r"\bfarmed pigs|\bpigs\b|\bporcine", "porcine"),
    (r"\bsheep|\blamb|\bovine\b", "ovine"),
    (r"\bgoats?\b|\bcaprine", "caprine"),
    (r"\bdeer\b|\bvenison", "cervid"),
    (r"\bpoultry|\bchicken|\bbroiler|\bduck|\bturkey", "poultry"),
    (r"\bhorse|\bequine", "equine"),
    (r"\brabbit|\bhare\b", "lagomorph"),
    (r"\bfish\b|\bseafood|\bshellfish|\bmollusc", "other"),
]


def parse_nz_mpi(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """New Zealand MPI Risk Measure Data Extract.

    The register covers every registered animal-product operation, so most rows
    are honey packers, cold stores and egg graders rather than abattoirs. That
    is the point: the extract says which is which, in a field, rather than
    leaving it to be guessed from a company name.
    """
    snapshot = snapshot or date.today().isoformat()
    out, seen = [], set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            rid = (row.get("MPI ID Number") or "").strip()
            name = ((row.get("Operator Trading Name") or "").strip()
                    or (row.get("Operator Business Name") or "").strip())
            if not name:
                continue
            if rid and rid in seen:
                continue
            seen.add(rid)

            proc = " ".join([(row.get("Primary") or ""),
                             (row.get("Secondary Process") or "")]).lower()
            prog = (row.get("Programme Type") or "").lower()
            acts = {a for needle, a in _NZ_PROCESS if needle in proc}
            acts |= {a for needle, a in _NZ_PROGRAMME if needle in prog}

            material = (row.get("Product/Material") or "").lower()
            if "layer" in material or "rearer" in material:
                acts.add("farm_eggs")
            species = [v for pat, v in _NZ_SPECIES
                       if re.search(pat, material) and v in SPECIES]

            out.append(SourceRecord(
                source_id="nz_mpi",
                source_snapshot=snapshot,
                source_row_id=rid or f"row{i}",
                name=name,
                country_iso3="NZL",
                national_id=rid or None,
                id_scheme="NZ-MPI",
                address=(row.get("Physical Address") or "").strip() or None,
                locality=(row.get("Physical Town/City") or "").strip() or None,
                admin1=((row.get("Location Local Authority") or "").strip()
                        or (row.get("Operator Local Authority") or "").strip()
                        or None),
                species=species,
                activities=sorted(acts) or ["unknown"],
                # Only the word Slaughter. Dressing on its own is a plant that
                # takes carcasses in, and the register distinguishes them.
                slaughter=True if "slaughter" in proc or "micro abattoir" in prog
                          else None,
                operator=(row.get("Operator Business Name") or "").strip() or None,
                raw={k: v for k, v in row.items() if v},
            ))
    return out


# ---------------------------------------------------------------------------
# Brazil SIF
# ---------------------------------------------------------------------------
#
# MAPA publishes the federal register as one semicolon-delimited CSV, and it is
# richer than any other national list in this pipeline: CATEGORIA_CLASSE names
# the kind of plant, the species and the throughput band in one string --
# "ABATEDOURO FRIGORÍFICO - C15 / AB2 - BOVINO - mais de 80/h".
#
# One row per habilitation event, not per plant. A single abattoir appears once
# for every export approval it has ever been granted or lost, so JBS Mozarlândia
# runs to a dozen rows. Rows are folded on the SIF number and the CNPJ, and the
# activities and species of every row for a plant are unioned -- a plant listed
# once as a slaughterhouse and once as a cutting plant is both.

_BR_ACTIVITY = [
    ("ABATEDOURO", "slaughter"),
    ("ABATE", "slaughter"),
    ("UNIDADE DE BENEF. DE CARNE", "processing"),
    ("UNIDADE DE BENEFICIAMENTO DE CARNE", "processing"),
    ("UNIDADE DE BENEF. DE PESCADO", "processing"),
    ("UNIDADE DE BENEFICIAMENTO DE PESCADO", "processing"),
    ("PRODUTOS DE ABELHAS", "farm_honey"),
    ("ENTREPOSTO", "cold_store"),
    ("GRANJA", "farm_eggs"),
    ("UNIDADE DE BENEF. DE OVOS", "egg_products"),
    ("OVOS", "egg_products"),
    ("LEITE", "farm_dairy"),
    ("CHARQUE", "processing"),
    ("CONSERVAS", "processing"),
]
# Matched on word boundaries, not substrings: "BOVINO" contains "OVIN", and a
# plain substring test filed every cattle abattoir in Brazil as sheep as well.
_BR_SPECIES = [
    (r"\bBOVIN|\bBUBALIN|\bBUFAL", "bovine"),
    (r"\bSU[IÍ]N|\bSUID", "porcine"),
    (r"\bAVES?\b|\bFRANGO|\bPERU\b", "poultry"),
    (r"\bOVIN", "ovine"),
    (r"\bCAPRIN", "caprine"),
    (r"\bEQU[IÍ]N|\bEQU[IÍ]D", "equine"),
    (r"\bCOELH|\bLAGOMORF", "lagomorph"),
    (r"\bPESCADO|\bPEIXE|\bRÃ\b", "other"),
]


def parse_br_sif(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """Brazilian federal register (SIGSIF). Federal inspection only.

    State (SIE) and municipal (SIM) inspected plants are separate registries
    and are the majority of Brazilian abattoirs by count. Nothing here says so
    on a per-record basis, so the gap belongs in the source note rather than in
    a flag that would imply this list is complete.
    """
    snapshot = snapshot or date.today().isoformat()
    by_plant: dict = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh, delimiter=";")):
            sif = (row.get("NR_SIF") or "").strip()
            cnpj = (row.get("CPF_CNPJ") or "").strip()
            name = (row.get("RAZAO_SOCIAL") or "").strip()
            if not (sif or cnpj) or not name:
                continue
            key = f"{sif}|{cnpj}"
            klass = ((row.get("CATEGORIA_CLASSE") or "") + " "
                     + (row.get("AREA_CATEGORIA") or "")).upper()
            acts = {a for needle, a in _BR_ACTIVITY if needle in klass}
            sp = {v for pat, v in _BR_SPECIES if re.search(pat, klass)}

            entry = by_plant.get(key)
            if entry is None:
                by_plant[key] = entry = {
                    "row": row, "i": i, "acts": set(), "species": set(),
                    "classes": set(),
                }
            entry["acts"] |= acts
            entry["species"] |= sp
            if klass.strip():
                entry["classes"].add((row.get("CATEGORIA_CLASSE") or "").strip())

    out = []
    for key, e in by_plant.items():
        row = e["row"]
        sif = (row.get("NR_SIF") or "").strip()
        street = ", ".join(x for x in ((row.get("LOGRADOURO") or "").strip(),
                                       (row.get("BAIRRO") or "").strip()) if x)
        out.append(SourceRecord(
            source_id="br_sif",
            source_snapshot=snapshot,
            source_row_id=key,
            name=(row.get("RAZAO_SOCIAL") or "").strip(),
            country_iso3="BRA",
            national_id=sif or None,
            id_scheme="BR-SIF",
            address=street or None,
            locality=(row.get("MUNICIPIO") or "").strip() or None,
            admin1=(row.get("UF") or "").strip() or None,
            postcode=(row.get("CEP") or "").strip() or None,
            species=sorted(sp for sp in e["species"] if sp in SPECIES),
            activities=sorted(e["acts"]) or ["unknown"],
            # "ABATEDOURO" in the class is the register saying animals are
            # killed here. Anything else is left unstated rather than denied:
            # the class names what the plant is approved for, and a cold store
            # approval is not a statement that nothing dies on the site.
            slaughter=True if "slaughter" in e["acts"] else None,
            operator=(row.get("NOME_FANTASIA") or "").strip() or None,
            raw={k: v for k, v in row.items() if v}
                | {"classes": " | ".join(sorted(e["classes"]))},
        ))
    return out


# ---------------------------------------------------------------------------
# Brazil, Trase's logistics map
# ---------------------------------------------------------------------------
#
# Trase (trase.earth, CC BY 4.0) compiles Brazil's federal register (SIF), the
# state registers (SIE), and the municipal and consortium inspection services
# that report through SISBI, and gives every site a position. It is the only
# source here for the state and municipal plants, which are most of Brazil's
# abattoirs by count and which the federal register above leaves out entirely.
# 18,090 rows on 20 September 2026: dairies, meat and fish processors, egg and
# honey units and stores as well as slaughterhouses. All of them are read.
#
# One row per facility AND commodity: a plant that handles beef and pork is two
# rows with the same facility_id. Rows are folded on facility_id, and the
# commodities, species and activities of every row for a site are unioned.
#
# WHAT A SITE DOES is read from Trase's own words for it (type_english and
# type_portuguese - the English column is only partly translated, so both
# languages are matched). A type none of the rules recognises is "unknown",
# with Trase's wording kept in raw; nothing is guessed from the company's name.
#
# WHICH ANIMALS comes from Trase's commodity column and from any species its
# type names ("Abatedouro frigorifico bovino e suino"). "Meat" and "Dairy" name
# no animal, and none is assigned.
#
# The cnpj column is kept as published. For a site registered to a person
# rather than a company it is that person's own tax number (11 digits rather
# than 14); the owner asked for it to be kept (20 September 2026).

_TRASE_ACTIVITY = [
    (r"slaughter|abatedouro|matadouro|\babate\b", "slaughter"),
    (r"meat processing|meat products factory|benef\w*\.? de carne|beneficiamento de carnes?"
     r"|produtos carneos|fabrica de produtos carneos|fabrica de conservas|canned"
     r"|charque|agro-?industrial processing|agroindustria", "processing"),
    (r"fish and seafood processing|fish slaughter and processing|benef\w*\.? de pescado"
     r"|beneficiamento de pescados?", "processing"),
    (r"milk and dairy processing|dairy factory|fabrica de laticinios|\blaticinios\b"
     r"|benef\w*\.? de leite|beneficiamento de leite|queijaria", "processing"),
    (r"dairy farm|estabulo leiteiro|granja leiteira", "farm_dairy"),
    (r"egg processing|benef\w*\.? de ovos|beneficiamento de ovos", "egg_products"),
    (r"bee products|prod\w*\.? de abelhas?|produtos (de|das) abelhas?", "farm_honey"),
    (r"refrigeration facility|storage and distribution|warehouse|entreposto", "cold_store"),
    (r"non-edible|nao comestiveis", "rendering"),
]
_TRASE_COMMODITY_SPECIES = {
    "beef": "bovine", "pork": "porcine", "chicken": "poultry", "egg": "poultry",
    "lamb": "ovine", "goat": "caprine", "rabbit": "lagomorph", "equine": "equine",
    "fish": "fish", "honey": "insect",
}
_TRASE_TYPE_SPECIES = [
    (r"\bbovin|\bbubalin|\bbovideos|\bcattle\b|\bbeef\b", "bovine"),
    (r"\bsuin|\bsuideos|\bswine\b|\bpork\b", "porcine"),
    (r"\baves?\b|\bpoultry\b|\bgalinha|\bfrango", "poultry"),
    (r"\bovin", "ovine"), (r"\bcaprin", "caprine"), (r"\bequin|\bequid", "equine"),
    (r"\bcoelh|\brabbit", "lagomorph"), (r"\bcrustace", "crustacean"),
    (r"\bpescado|\bfish\b|\bpeixe", "fish"), (r"\babelha|\bbee\b", "insect"),
]


def _trase_activities(type_words: str, commodity: str) -> set:
    from normalize import norm_text
    words = norm_text(type_words).replace(" - ", " ")
    raw = (type_words or "").lower()
    acts = {a for pat, a in _TRASE_ACTIVITY if re.search(pat, words) or re.search(pat, raw)}
    # Trase's "Poultry farm" is a laying farm where its commodity says eggs, and
    # otherwise does not say what the birds are kept for.
    if re.search(r"poultry farm|granja avicola", words):
        acts.add("farm_eggs" if commodity == "egg" else "farm_poultry")
    return acts


def parse_br_trase(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """Trase's Brazilian logistics map: every row, folded to one record per site."""
    from normalize import norm_text
    snapshot = snapshot or date.today().isoformat()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    feats = data.get("features")
    if not isinstance(feats, list) or not feats:
        raise HeaderError(f"{path} holds no features; is it Trase's .geo.json?")
    need = {"facility_id", "company", "commodity", "inspection_level", "lat", "long"}
    have = set((feats[0].get("properties") or {}).keys())
    if not need <= have:
        raise HeaderError(f"Trase's file has lost {sorted(need - have)}; it now carries {sorted(have)}")

    sites: dict = {}
    for i, ft in enumerate(feats):
        p = ft.get("properties") or {}
        fid = str(p.get("facility_id") or p.get("unique_id") or f"row{i}")
        e = sites.get(fid)
        if e is None:
            sites[fid] = e = {"first": p, "geom": ft.get("geometry"), "rows": [], "acts": set(),
                              "species": set(), "commodities": [], "types": []}
        e["rows"].append(p)
        commodity = str(p.get("commodity") or "").strip()
        type_words = " ".join(str(p.get(k) or "") for k in ("type_english", "type_portuguese"))
        e["acts"] |= _trase_activities(type_words, commodity.lower())
        if commodity and commodity not in e["commodities"]:
            e["commodities"].append(commodity)
        for k in ("type_english", "type_portuguese"):
            if p.get(k) and p[k] not in e["types"]:
                e["types"].append(p[k])
        sp = _TRASE_COMMODITY_SPECIES.get(commodity.lower())
        if sp:
            e["species"].add(sp)
        folded = norm_text(type_words)
        e["species"] |= {v for pat, v in _TRASE_TYPE_SPECIES if re.search(pat, folded)}

    out = []
    for fid, e in sites.items():
        p = e["first"]
        name = str(p.get("company") or "").strip()
        if not name:
            # Nothing to call it. Kept all the same, under Trase's own id for it.
            name = fid
        lat, lon = p.get("lat"), p.get("long")
        if (lat is None or lon is None) and e["geom"] and e["geom"].get("coordinates"):
            lon, lat = e["geom"]["coordinates"][:2]
        level = str(p.get("inspection_level") or "").strip().upper()
        num = p.get("inspection_num")
        try:
            num = str(int(float(num))) if num is not None else None
        except (TypeError, ValueError):
            num = str(num).strip() or None
        # Only the federal number is shared with another register here (br_sif),
        # so only it is offered to the matcher as that register's number. State
        # and municipal numbers restart in every state and town, so a bare
        # "SIE 12" would join plants a thousand kilometres apart; those sites
        # carry Trase's own id instead, which is unique to the site.
        is_sif = level == "SIF" and num
        raw = {k: v for k, v in p.items() if v not in (None, "")}
        raw["commodities"] = " | ".join(e["commodities"])
        raw["types"] = " | ".join(e["types"])
        if len(e["rows"]) > 1:
            # What differs between this site's rows (its capacity per commodity, say), so nothing Trase publishes is lost in the fold.
            keys = sorted({k for r in e["rows"] for k in r if any(r.get(k) != e["rows"][0].get(k) for r in e["rows"])})
            raw["rows"] = [{k: r.get(k) for k in keys if r.get(k) not in (None, "")} for r in e["rows"]]
        out.append(SourceRecord(
            source_id="br_trase",
            source_snapshot=snapshot,
            source_row_id=fid,
            name=name,
            country_iso3="BRA",
            national_id=num if is_sif else fid,
            id_scheme="BR-SIF" if is_sif else "BR-TRASE",
            foreign_id=fid if is_sif else None,
            foreign_id_scheme="BR-TRASE" if is_sif else None,
            locality=str(p.get("municipality") or "").strip() or None,
            admin1=str(p.get("state") or "").strip() or None,
            species=sorted(s for s in e["species"] if s in SPECIES),
            activities=sorted(e["acts"]) or ["unknown"],
            # Trase calling a site a slaughterhouse is Trase saying animals are
            # killed there. Any other type is left unstated rather than denied.
            slaughter=True if "slaughter" in e["acts"] else None,
            src_lat=float(lat) if lat is not None else None,
            src_lon=float(lon) if lon is not None else None,
            raw=raw,
        ))
    shared = {}
    for r in out:
        if r.src_lat is not None:
            shared.setdefault((round(r.src_lat, 5), round(r.src_lon, 5)), []).append(r)
    crowded = sum(len(v) for v in shared.values() if len(v) > 1)
    print(f"  Trase: {len(feats):,} rows folded to {len(out):,} sites; "
          f"{sum(1 for r in out if r.id_scheme == 'BR-SIF'):,} carry a federal (SIF) number; "
          f"{sum(1 for r in out if r.activities == ['unknown']):,} have a type no rule recognises; "
          f"{crowded:,} share their exact position with another site")
    return out


# ---------------------------------------------------------------------------
# China GACC / CIFER
# ---------------------------------------------------------------------------

def parse_cifer(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """CIFER query results, saved as JSON lines by the retrieval script.

    The field that earns CIFER its place here is the foreign establishment
    number: it is the home authority's own number, which is what lets a Chinese
    registration collapse onto the USDA, DAFF or EU record for the same plant
    instead of becoming a second pin.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            cn_no = r.get("registerNo") or r.get("cn_register_no")
            fo_no = r.get("foreignRegisterNo") or r.get("foreign_no")
            country = (to_iso3(r.get("_country_iso3"))
                       or to_iso3(r.get("countryCode"))
                       or to_iso3(r.get("country")) or "")
            cats = " ".join(str(c) for c in (r.get("productCategory") or r.get("categories") or []))

            # Only claim slaughter when the category text says so. Meat
            # categories cover cold stores and cutting plants too.
            low = cats.lower()
            slaughter = True if ("slaughter" in low or "屠宰" in cats) else None

            # Species comes from the category the row was queried under, which
            # the browser harvester records as `_species`. The response text
            # itself never names a species, so querying subcategory by
            # subcategory is what makes this field exist at all.
            species = list(r.get("_species") or [])
            if not species:
                for kw, grp in (("beef", "bovine"), ("bovine", "bovine"),
                                ("cattle", "bovine"), ("pork", "porcine"),
                                ("swine", "porcine"), ("porcine", "porcine"),
                                ("poultry", "poultry"), ("chicken", "poultry"),
                                ("mutton", "ovine"), ("sheep", "ovine"),
                                ("lamb", "ovine"), ("goat", "caprine"),
                                ("horse", "equine")):
                    if kw in low:
                        species.append(grp)

            out.append(SourceRecord(
                source_id="cifer_china",
                source_snapshot=snapshot,
                source_row_id=str(cn_no or r.get("id")),
                name=r.get("enName") or r.get("name") or str(cn_no),
                country_iso3=country,
                national_id=str(cn_no) if cn_no else None,
                id_scheme="CN-CIFER",
                foreign_id=str(fo_no) if fo_no else None,
                foreign_id_scheme=_cifer_foreign_scheme(country),
                address=r.get("address") or r.get("enAddress"),
                species=sorted(set(species)),
                activities=["unknown"],
                slaughter=slaughter,
                raw=r,
            ))
    return out


def _cifer_foreign_scheme(country_iso3: str) -> str | None:
    """Which national scheme a CIFER foreign number belongs to.

    Only mapped where the correspondence is unambiguous. Everywhere else the
    number is still stored, just not used as a cross-registry join key -- a
    wrong scheme would merge two unrelated plants that happen to share a
    number, which is exactly the failure this pipeline exists to avoid.
    """
    return {
        "USA": "US-FSIS",
        "AUS": "AU-EXPORT",
        "NZL": "NZ-MPI",
        "BRA": "BR-SIF",
        "CAN": "CA-SFC",
    }.get(country_iso3)


# ---------------------------------------------------------------------------
# OpenStreetMap
# ---------------------------------------------------------------------------

def parse_osm(path: Path, snapshot: str | None = None) -> list[SourceRecord]:
    """Overpass JSON. Contributes coordinates, not registry identity.

    OSM records deliberately carry slaughter=True only when the tag says so;
    the tags in the query are all explicit slaughterhouse tags, so that holds.
    """
    snapshot = snapshot or date.today().isoformat()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        name = tags.get("name") or tags.get("operator") or "(unnamed OSM site)"
        street = " ".join(x for x in (tags.get("addr:housenumber"),
                                      tags.get("addr:street")) if x)
        out.append(SourceRecord(
            source_id="osm_overpass",
            source_snapshot=snapshot,
            source_row_id=f"{el['type']}/{el['id']}",
            name=name,
            # addr:country is present on a small minority of OSM elements, and
            # a blank country puts every one of them in the same dedup block
            # and outside every country filter. The coordinate is always there,
            # so it answers the question instead.
            country_iso3=(to_iso3(tags.get("addr:country"))
                          or country_at(lat, lon) or ""),
            national_id=None,
            id_scheme="OSM",
            address=street or None,
            locality=tags.get("addr:city"),
            postcode=tags.get("addr:postcode"),
            species=[],
            activities=["slaughter"],
            slaughter=True,
            operator=tags.get("operator"),
            src_lat=float(lat), src_lon=float(lon),
            raw=tags,
        ))
    return out


# ---------------------------------------------------------------------------
# Generic tabular fallback
# ---------------------------------------------------------------------------

def parse_generic(path: Path, *, source_id: str, id_scheme: str,
                  country_iso3: str, snapshot: str | None = None,
                  slaughter_default: bool | None = None) -> list[SourceRecord]:
    """For national registries whose layout has not been given a parser yet.

    slaughter_default exists for lists that are slaughterhouse-only by
    definition, where the file has no flag column because every row is one.
    Set it explicitly per source; it is never guessed.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        h = rd.fieldnames or []
        c_num = _resolve(h, "number", "id", "registration", "licence", "license",
                         "approval", required=False)
        c_name = _resolve(h, "name", "establishment", "company", "operator")
        c_addr = _resolve(h, "address", "street", "location", required=False)
        c_city = _resolve(h, "city", "town", "municipality", "locality", required=False)
        c_pc = _resolve(h, "postcode", "postalcode", "zip", "cep", required=False)
        c_region = _resolve(h, "state", "province", "region", "uf", required=False)

        for i, row in enumerate(rd):
            name = _f(row, c_name)
            if not name:
                continue
            out.append(SourceRecord(
                source_id=source_id,
                source_snapshot=snapshot,
                source_row_id=_f(row, c_num) or f"row{i}",
                name=name,
                country_iso3=country_iso3,
                national_id=_f(row, c_num),
                id_scheme=id_scheme,
                address=_f(row, c_addr),
                locality=_f(row, c_city),
                admin1=_f(row, c_region),
                postcode=_f(row, c_pc),
                species=[],
                activities=["slaughter"] if slaughter_default else ["unknown"],
                slaughter=slaughter_default,
                raw={k: v for k, v in row.items() if v},
            ))
    return out


# ---------------------------------------------------------------------------
# Farm Transparency Project
# ---------------------------------------------------------------------------

# FTP category -> activity. The point of carrying all of them rather than
# filtering to slaughterhouses is that the killing is one stage of a system;
# the farms, saleyards and holding yards are where the same animals spend the
# rest of their lives, and the map can separate them on demand.
_FTP_ACTIVITY = {
    "slaughterhouse": "slaughter",
    "knackery": "slaughter",
    "farm (meat)": "farm_meat",        "meat farm": "farm_meat",
    "farm (dairy)": "farm_dairy",      "dairy farm": "farm_dairy",
    "farm (eggs)": "farm_eggs",        "egg farm": "farm_eggs",
    "farm (wool)": "farm_wool",        "wool farm": "farm_wool",
    "farm (skins/fur)": "farm_skins",  "fur farm": "farm_skins",
    "honey farm": "farm_honey",
    "sheep farm": "farm_meat",
    "hatchery": "hatchery",
    "saleyard": "saleyard",
    "live market": "live_market",
    "depot / holding yard": "holding_yard",
    "experimentation": "experimentation",
    "zoo": "zoo",
    "wildlife": "wildlife",
    "race training/breeding": "racing",
    "race training/breeding facility": "racing",
    "racecourse": "racing",
    "rodeo": "rodeo",
    "other entertainment": "entertainment",
    "agricultural show": "agricultural_show",
    "pet breeder": "pet_breeder",
    "pet shop": "pet_shop",
    "rendering plant": "rendering",
    "meat processing (non-slaughter)": "processing",
    "aquaculture": "aquaculture",
    # Longer wordings used in the KML export of the same data.
    "farm (honey)": "farm_honey",
    "skin/fur farm": "farm_skins",
    "meat processing (non-slaughter) facility": "processing",
    "scientific experimentation facility": "experimentation",
    "other/misc entertainment facility": "entertainment",
    "egg processing and distribution factory": "processing",
    "dairy processing factory": "processing",
    "dairy processing": "processing",
    "egg processing and distribution": "processing",
    "circus": "entertainment",
    "live animal export facility": "holding_yard",
    "knackery / rendering": "slaughter",
}

_FTP_SPECIES = {
    "cows/cattle": "bovine", "cattle": "bovine", "cows": "bovine",
    "pigs": "porcine", "chickens": "poultry", "turkeys": "poultry",
    "ducks": "poultry", "geese": "poultry", "quail": "poultry",
    "misc birds": "poultry", "pigeons": "poultry", "emus": "poultry",
    "ostriches": "poultry", "chicks": "poultry",
    "sheep": "ovine", "lambs": "ovine", "goats": "caprine",
    "horses": "equine", "donkeys": "equine", "greyhounds": "canine",
    "dogs": "canine", "cats": "other", "rabbits": "lagomorph",
    "minks": "mustelid", "ferrets": "mustelid", "foxes": "other",
    "deer": "cervid", "kangaroos": "wild_game", "camels": "other",
    "alpacas": "camelid", "llamas": "camelid", "buffalo": "bovine",
    "fish/sealife": "fish", "fish": "fish", "prawns": "crustacean",
    "crocodiles": "reptile", "alligators": "reptile", "bees": "insect",
}


def _ftp_split(value: str | None) -> list[str]:
    """FTP joins multiple values with commas, but a value may itself contain a
    comma inside brackets -- "Farm (meat) (unconfirmed)". Split on commas that
    are not inside brackets."""
    if not value:
        return []
    return [p.strip() for p in re.split(r",(?![^(]*\))", value) if p.strip()]


def _ftp_map(values: list[str], table: dict, strip_unconfirmed: bool = True):
    out, unknown = [], []
    for v in values:
        k = v.lower().strip()
        if strip_unconfirmed:
            k = k.replace("(unconfirmed)", "").strip()
        if k in table:
            out.append(table[k])
        else:
            unknown.append(v)
    return sorted(set(out)), unknown


def parse_ftp_csv(path: Path, *, country_iso3: str | None = None,
                  snapshot: str | None = None) -> list[SourceRecord]:
    """Farm Transparency Project CSV export.

    Every row carries a coordinate the project verified, so these records skip
    geocoding entirely -- which is why the CSV export is worth asking for in
    preference to the KML.
    """
    snapshot = snapshot or date.today().isoformat()
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        h = rd.fieldnames or []
        c_id = _resolve(h, "id")
        c_name = _resolve(h, "name")
        c_cat = _resolve(h, "categories", "category")
        c_sp = _resolve(h, "species", required=False)
        c_lat = _resolve(h, "lat", "latitude")
        c_lon = _resolve(h, "lng", "lon", "longitude")
        c_num = _resolve(h, "streetnum", "street num", required=False)
        c_st = _resolve(h, "street", required=False)
        c_sub = _resolve(h, "suburb", "city", required=False)
        c_state = _resolve(h, "state", required=False)
        c_pc = _resolve(h, "postcode", "zip", required=False)
        c_ctry = _resolve(h, "country", required=False)
        c_stat = _resolve(h, "lastknownstatus", "status", required=False)
        c_own = _resolve(h, "ownedby", "owned by", required=False)
        c_con = _resolve(h, "contractedto", required=False)

        for row in rd:
            lat = lon = None
            try:
                lat, lon = float(row[c_lat]), float(row[c_lon])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    lat = lon = None
            except (TypeError, ValueError, KeyError):
                lat = lon = None

            acts, unknown_cats = _ftp_map(_ftp_split(_f(row, c_cat)), _FTP_ACTIVITY)
            species, _ = _ftp_map(_ftp_split(_f(row, c_sp)), _FTP_SPECIES)

            street = " ".join(x for x in (_f(row, c_num), _f(row, c_st)) if x)
            addr = ", ".join(x for x in (street, _f(row, c_sub), _f(row, c_state)) if x)

            out.append(SourceRecord(
                source_id="farm_transparency",
                source_snapshot=snapshot,
                source_row_id=_f(row, c_id) or f"{lat},{lon}",
                name=_f(row, c_name) or "(unnamed)",
                country_iso3=(to_iso3(country_iso3) or to_iso3(_f(row, c_ctry)) or ""),
                national_id=_f(row, c_id),
                id_scheme="FTP",
                address=addr or None,
                locality=_f(row, c_sub),
                admin1=_f(row, c_state),
                postcode=_f(row, c_pc),
                species=species,
                activities=acts or ["unknown"],
                # Only "slaughterhouse" and "knackery" mean animals are killed
                # here. Everything else is left unstated rather than denied --
                # a meat farm is not a slaughterhouse, but FTP does not assert
                # that no animal ever dies on it.
                slaughter=True if "slaughter" in acts else None,
                operator=_f(row, c_own) or _f(row, c_con),
                src_lat=lat, src_lon=lon,
                raw={k: v for k, v in row.items() if v}
                    | ({"_unmapped_categories": unknown_cats} if unknown_cats else {}),
            ))
    return out


# Country names now resolve through countries.to_iso3, which covers every
# name the sources publish rather than the eight that were listed here.


def parse_ftp_kml(path: Path, *, country_iso3: str = "AUS",
                  snapshot: str | None = None) -> list[SourceRecord]:
    """Farm Transparency KML/GPX export.

    Same project, worse format: everything but the coordinate is packed into a
    prose description that has to be pulled apart. Use the CSV export where the
    project offers one.
    """
    snapshot = snapshot or date.today().isoformat()
    text = Path(path).read_text(encoding="utf-8", errors="replace")

    blocks = re.findall(
        r'<Placemark id="placemark([^"]+)">\s*<name>(.*?)</name>\s*'
        r'<description>(.*?)</description>.*?'
        r'<coordinates>\s*([-0-9.]+)\s*,\s*([-0-9.]+)',
        text, re.S)

    out = []
    for fid, name, desc, lon, lat in blocks:
        def field(label):
            # [^\n]* rather than a lazy .*? -- when the field is empty, a
            # leading \s* swallows the newline and the pattern captures the
            # NEXT line's value instead. That silently filed a thousand
            # "Last known status: Open and operating" strings as categories.
            m = re.search(rf"{label}:[ \t]*([^\n]*)", desc)
            if not m:
                return None
            v = m.group(1).strip()
            return v or None

        # The name repeats the categories and id in brackets; strip them so the
        # display name is the facility, not a restatement of its metadata.
        clean = re.sub(r"\s*\([^()]*#[0-9a-f]+\)\s*$", "", name).strip()

        acts, unknown = _ftp_map(_ftp_split(field("Categories")), _FTP_ACTIVITY)
        species, _ = _ftp_map(_ftp_split(field("Species")), _FTP_SPECIES)
        address = field("Address")

        try:
            flat, flon = float(lat), float(lon)
        except ValueError:
            continue

        out.append(SourceRecord(
            source_id="farm_transparency",
            source_snapshot=snapshot,
            source_row_id=fid,
            name=clean or name,
            country_iso3=country_iso3,
            national_id=fid,
            id_scheme="FTP",
            address=address,
            species=species,
            activities=acts or ["unknown"],
            slaughter=True if "slaughter" in acts else None,
            src_lat=flat, src_lon=flon,
            raw={"description": desc, "status": field("Last known status")}
                | ({"_unmapped_categories": unknown} if unknown else {}),
        ))
    return out
