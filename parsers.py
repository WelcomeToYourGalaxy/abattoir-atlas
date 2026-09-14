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

from countries import to_iso3
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
            country_iso3=to_iso3(tags.get("addr:country")) or "",
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
