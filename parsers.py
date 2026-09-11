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

from schema import SourceRecord


class HeaderError(RuntimeError):
    pass


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
                lv = _truthy(d.get(_resolve(demo_headers, "livestock_slaughter",
                                            "livestockslaughter", required=False) or ""))
                pv = _truthy(d.get(_resolve(demo_headers, "poultry_slaughter",
                                            "poultryslaughter", required=False) or ""))
                votes = [v for v in (lv, pv) if v is not None]
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
                size_class=_f(row, c_size),
                operator=_f(row, c_dba),
                src_lat=lat, src_lon=lon,
                raw={k: v for k, v in row.items() if v} | {"_demographic": bool(d)},
            ))
    return out


# ---------------------------------------------------------------------------
# EU: third-country lists and member-state lists
# ---------------------------------------------------------------------------

# Annex III to Reg. (EC) 853/2004. Section determines species; the activity
# code determines whether the plant actually kills.
_EU_SECTION_SPECIES = {
    "I": ["bovine", "porcine", "ovine", "caprine", "equine"],
    "II": ["poultry", "lagomorph"],
    "III": ["farmed_game"],
    "IV": ["wild_game"],
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

        for i, row in enumerate(rd):
            num = _f(row, c_num)
            name = _f(row, c_name)
            if not (num or name):
                continue

            section = (_f(row, c_section) or "").strip().upper()
            section = re.sub(r"[^IVX]", "", section)
            species = list(_EU_SECTION_SPECIES.get(section, []))

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

            out.append(SourceRecord(
                source_id=source_id,
                source_snapshot=snapshot,
                source_row_id=num or f"row{i}",
                name=name or num,
                country_iso3=(country_iso3 or _f(row, c_country) or "").upper()[:3],
                national_id=num,
                id_scheme=id_scheme,
                address=_f(row, c_addr),
                locality=_f(row, c_city),
                admin1=_f(row, c_region),
                postcode=_f(row, c_pc),
                species=species,
                activities=sorted(acts) or ["unknown"],
                slaughter=slaughter,
                raw={k: v for k, v in row.items() if v} | {"section": section,
                                                           "remark": _f(row, c_remark)},
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
            country = (r.get("countryCode") or r.get("country") or "").upper()[:3]
            cats = " ".join(str(c) for c in (r.get("productCategory") or r.get("categories") or []))

            # Only claim slaughter when the category text says so. Meat
            # categories cover cold stores and cutting plants too.
            low = cats.lower()
            slaughter = True if ("slaughter" in low or "屠宰" in cats) else None

            species = []
            for kw, grp in (("beef", "bovine"), ("bovine", "bovine"), ("cattle", "bovine"),
                            ("pork", "porcine"), ("swine", "porcine"), ("porcine", "porcine"),
                            ("poultry", "poultry"), ("chicken", "poultry"),
                            ("mutton", "ovine"), ("sheep", "ovine"), ("lamb", "ovine"),
                            ("goat", "caprine"), ("horse", "equine")):
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
            country_iso3=(tags.get("addr:country") or "").upper()[:3],
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
