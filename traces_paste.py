#!/usr/bin/env python3
"""
Turn pasted TRACES-NT establishment listings into CSV.

The TRACES directory renders a table that copies out as a flattened mess: the
first four fields tab-separated on one line, then region, activity codes and
dates on their own lines underneath. This reads that shape back into rows.

Workflow:
  1. Open a country's listing in TRACES-NT, select all, copy.
  2. Paste into a .txt file in raw/traces_paste/  (one file per country; the
     filename does not matter, the country is read from the header line).
  3. python traces_paste.py
  4. CSVs land in raw/eu_traces_third_country/, ready for parse_all.sh.

Which sections to pull. TRACES indexes by commodity code, not by Annex III
section number:

    RM   Meat of domestic ungulates       (Annex III Section I)
    PM   Meat from poultry and lagomorphs (Section II)
    GM   Meat of farmed game              (Section III)
    WM   Wild game meat                   (Section IV)

Each appears twice in the picker -- under Food (EFTA, European Union) for
member states and under Food (Third countries) for everyone else. Pull both.

Also worth pulling, if you want every kill floor regardless of what the meat
becomes:

    ABP-SH  Slaughterhouses and fishery vessels (animal by-products register)

That one catches plants killing for pet food and rendering, which never appear
in the human-consumption registers. It has no SH/CP column, and some of its
entries (rendering works, petfood plants) do not kill at all, so its rows are
recorded as slaughter-unstated rather than confirmed. The map keeps them
visible and separable instead of guessing.

Not RPM (meat products), MM (minced meat and preparations), CAS (casings), FAT
or MMP: nothing is killed at any of those, and including them would inflate the
atlas with processing plants.

Within an RM or PM listing the Activities column carries SH for slaughterhouses
and CP for cutting plants. This tool reads those codes per row rather than
assuming, so a cutting plant never lands in the data as a kill floor.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

SRC = Path("raw/traces_paste")
DST = Path("raw/eu_traces_third_country")

# Field separator. TRACES copies as tabs, but saving through an editor or a
# word processor turns each tab into a run of spaces -- and a tab-only parser
# reads such a file as zero rows without complaining. Two or more spaces is
# safe: names, streets and species lines all use single spaces internally.
SEP_RE = re.compile(r"\t|  +")

COUNTRY_RE = re.compile(r"^Country:\s*(.+?)\s*$", re.M)
SECTION_HDR_RE = re.compile(r"List view\s*\(\s*(.+?)\s*-\s*(.+?)\s*\)")
SECTION_CODE_RE = re.compile(
    r"\b(ABP-[A-Z]+|RM|PM|GM|WM|RPM|MM|CAS|EEP|EPP|FFP|GEL|HON|LBM|MMP|FAT|"
    r"FLS|SPR|COL|RCG|TCG|GEN)\b")

# Activity codes as they appear in the Activities column, e.g. "SH - Slaughterhouse".
ACTIVITY_RE = re.compile(r"^([A-Z]{2,4})\s*[-–]\s*(.+)$")

# The Remarks column lists species one per line as a letter and a Latin name,
# with no dash: "B Bovinae", "P Suidae". Without this they fall through to the
# region field and the species data is lost -- which is the whole reason to
# prefer RM/PM over the by-products register.
SPECIES_RE = re.compile(
    r"^([BCOSP])\s+(Bovinae|Capra hircus|Ovis aries|Equidae|Suidae)\s*$")
SPECIES_MAP = {
    "Bovinae": "bovine",
    "Suidae": "porcine",
    "Ovis aries": "ovine",
    "Capra hircus": "caprine",
    "Equidae": "equine",
}

# Which activity codes mean animals are killed on site. Only these.
SLAUGHTER_CODES = {"SH"}

# Sections this atlas cares about. Anything else is recorded but flagged, so a
# mistaken pull is visible rather than silently mixed in.
MEAT_SECTIONS = {"RM", "PM", "GM", "WM", "FFP", "ABP-SH"}

# The section picker offers every commodity code twice: once under Food (EFTA,
# European Union) and once under Food (Third countries). Both land in the same
# folder, and the filename cannot tell them apart -- so the group is derived
# from membership and written into the row. Northern Ireland is the reason this
# has to be a lookup rather than a guess: it sits in the EU group under the
# Windsor Framework despite not being a member state.
EU_EFTA = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "denmark",
    "estonia", "finland", "france", "germany", "greece", "hungary", "ireland",
    "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands",
    "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
    "iceland", "liechtenstein", "norway", "switzerland",
    "united kingdom (northern ireland)",
}


SECTION_BY_NAME = {
    "meat of domestic ungulates": "RM",
    "meat from poultry and lagomorphs": "PM",
    "meat of farmed game": "GM",
    "wild game meat": "WM",
    "fishery products": "FFP",
    "fishery products - eu": "FFP",
    "slaughterhouses and fishery vessels": "ABP-SH",
    "meat products": "RPM",
}


def group_for(country: str) -> str:
    return "EU-EFTA" if country.strip().lower() in EU_EFTA else "third-country"

# Sections whose vocabulary has no SH/CP distinction. A row here is neither
# confirmed nor denied as a slaughterhouse, and saying "N" would be a claim the
# source never made.
NO_ACTIVITY_VOCAB = {"ABP-SH", "ABP-PROCP", "ABP-PET", "ABP-INTP", "FFP"}
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
CAT_RE = re.compile(r"^CAT\d\b|^[A-Z]{2,4}\s*-\s*", re.I)
# Page chrome that can survive into a block when a copy starts mid-page.
CHROME = {"faq", "contact", "directory", "publications", "log in", "search",
          "english", "documentation", "home", "*", "-", "."}

NOISE = (
    "Skip to Main Content", "Documentation", "European Commission", "IMSOC",
    "TRACES", "Establishment list", "Details", "Last update", "Establishments",
    "Approval number", "Legal Notice", "Terms of Use", "Cookies",
    "Privacy Statement", "Accessibility", "Credits", "Contact", "Top Page",
    "processed by", "Log in", "English (English)", "Section:", "ago.",
)


def split_documents(text: str) -> list[str]:
    """One paste may hold several countries back to back."""
    marks = [m.start() for m in re.finditer(r"Skip to Main Content", text)]
    if len(marks) <= 1:
        return [text]
    marks.append(len(text))
    return [text[marks[i]:marks[i + 1]] for i in range(len(marks) - 1)]


HEADER_CELLS = {"approval number", "name", "street", "city", "region",
                "activities", "remarks", "date of approval", "publication date"}


def parse_vertical(text: str) -> list[dict]:
    """Second copy shape: one cell per line, tab-indented, records separated by
    blank lines, with the column header repeating every few records.

    The same TRACES table produces this instead of one-row-per-line depending on
    how the selection is made. Cells arrive in fixed order -- approval, name,
    street, city -- and everything after that is bulleted region, activity and
    species lines, which are told apart by shape rather than by position.
    """
    lines = []
    for raw in text.splitlines():
        s = raw.strip().lstrip("\t").strip()
        if s.lower().rstrip(" *") in HEADER_CELLS:
            continue
        if any(n in raw for n in NOISE):
            continue
        lines.append(s)

    blocks, cur = [], []
    for s in lines:
        if not s:
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(s)
    if cur:
        blocks.append(cur)

    rows = []
    for b in blocks:
        if len(b) < 4:
            continue
        approval, name, street, city = b[0], b[1], b[2], b[3]
        # The column header repeats every few records in this layout, which can
        # leave a truncated block at the seam. A real establishment has all
        # three of number, name and city; anything less is that artefact.
        if not (approval and name and city):
            continue
        # Navigation text can land in a block when the copy starts mid-page.
        # A real establishment name has letters and is not a menu item.
        bare = name.strip("*. ").lower()
        if bare in CHROME or len(bare) < 2 or approval.strip("*. ").lower() in CHROME:
            continue
        region, activities, species = [], [], []
        for s in b[4:]:
            s = s.lstrip("* ").rstrip(",").strip()
            if not s:
                continue
            sp = SPECIES_RE.match(s)
            if sp:
                species.append(SPECIES_MAP[sp.group(2)])
            elif ACTIVITY_RE.match(s) or CAT_RE.match(s):
                activities.append(s)
            else:
                region.append(s)
        rows.append({
            "approval_number": approval,
            "name": name,
            "address": street if street != "." else "",
            "city": city,
            "region": ", ".join(region),
            "activities": activities,
            "species": species,
            "dates": [],
        })
    return rows


def parse_document(text: str, fallback_country: str = "") -> tuple[str, str, list[dict]]:
    if "Loading more" in text:
        print("  !! this listing was still loading when copied -- scroll to the "
              "bottom and re-copy, or rows are missing", file=sys.stderr)
    country = ""
    m = COUNTRY_RE.search(text)
    if m:
        country = m.group(1).strip()
    if not country:
        m = SECTION_HDR_RE.search(text)
        if m:
            country = m.group(1).strip()
    if not country:
        # A block copied without its page header has no country line. Fall back
        # to the filename rather than leaving the field empty, which would strip
        # the rows of the one attribute the atlas cannot recover later.
        country = fallback_country.replace("-", " ").replace("_", " ").title()

    section = ""
    # The header line "List view ( Country - Section )" is the reliable source;
    # a bare code search can pick up a stray two-letter token from an address.
    m = SECTION_HDR_RE.search(text)
    if m:
        section = SECTION_BY_NAME.get(m.group(2).strip().lower(), "")
    if not section:
        m = SECTION_CODE_RE.search(text)
        if m:
            section = m.group(1).strip()

    rows, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if any(n in line for n in NOISE):
            continue

        fields = [f.strip() for f in SEP_RE.split(line)]
        if len(fields) >= 4 and fields[0]:
            # A new establishment: approval number, name, street, city.
            if cur:
                rows.append(cur)
            parts = fields
            parts = [p for p in parts]
            parts += [""] * (4 - len(parts)) if len(parts) < 4 else []
            cur = {
                "approval_number": parts[0],
                "name": parts[1],
                "address": parts[2] if parts[2] not in (".", "") else "",
                "city": parts[3],
                "region": "",
                "activities": [],
                "species": [],
                "dates": [],
            }
            continue

        if cur is None:
            continue

        s = line.strip()
        sp = SPECIES_RE.match(s)
        if DATE_RE.match(s):
            cur["dates"].append(s)
        elif sp:
            cur["species"].append(SPECIES_MAP[sp.group(2)])
        elif ACTIVITY_RE.match(s) or CAT_RE.match(s):
            cur["activities"].append(s)
        elif not cur["region"]:
            cur["region"] = s
        else:
            cur["region"] += "; " + s

    if cur:
        rows.append(cur)

    if not rows:
        rows = parse_vertical(text)

    out = []
    for r in rows:
        if not r["name"]:
            continue
        # Postcode: TRACES prefixes it to the city, e.g. "3250 Colac".
        # TRACES prefixes the postcode to the city: "1233 Bernex", "108
        # Reykjavik", "MRS1123 Marsa". Requiring a leading digit would strand
        # Malta, the UK, the Netherlands and Canada, whose codes start with
        # letters -- so the test is "contains a digit and is short", which no
        # city name satisfies.
        city, postcode = r["city"], ""
        pm = re.match(r"^(\S+)\s+(.+)$", city)
        if pm:
            head = pm.group(1)
            if (any(c.isdigit() for c in head)
                    and len(head) <= 10
                    and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z -]*", head)):
                postcode, city = head, pm.group(2)
                # Some codes are two tokens: NL "1234 AB", UK "SW1A 1AA".
                # Take the second token only when it looks like the tail of a
                # postcode rather than the start of a place name.
                nxt = re.match(r"^(\S+)\s+(.+)$", city)
                if nxt:
                    tail, rest = nxt.group(1), nxt.group(2)
                    nl_style = head.isdigit() and len(head) == 4 and \
                        re.fullmatch(r"[A-Z]{2}", tail)
                    uk_style = re.fullmatch(r"[A-Z]{1,2}[0-9][0-9A-Z]?", head) and \
                        re.fullmatch(r"[0-9][A-Z]{2}", tail)
                    if nl_style or uk_style:
                        postcode, city = f"{head} {tail}", rest
        # Real activity codes off each row. No assumption that a listing of
        # meat establishments is a listing of slaughterhouses -- most rows in an
        # RM or PM section are cutting plants.
        codes = []
        for a in r["activities"]:
            m2 = ACTIVITY_RE.match(a)
            if m2:
                codes.append(m2.group(1).upper())
        if any(c in ("SH", "CP") for c in codes):
            slaughter = "Y" if any(c in SLAUGHTER_CODES for c in codes) else "N"
        elif section in NO_ACTIVITY_VOCAB or not codes:
            slaughter = ""          # the listing simply does not say
        else:
            slaughter = "N"

        out.append({
            "approval_number": r["approval_number"],
            "name": r["name"],
            "address": r["address"],
            "city": city,
            "postcode": postcode,
            "region": r["region"],
            "country": country,
            "group": group_for(country),
            "section": section,
            "activity": " ".join(codes),
            "slaughter": slaughter,
            # Per-row species, straight from the Remarks column. Far better than
            # inferring from the section, which can only say "some ungulate".
            "species": " ".join(dict.fromkeys(r["species"])),
            "remarks": " | ".join(r["activities"]),
            "date_of_approval": r["dates"][0] if r["dates"] else "",
            "publication_date": r["dates"][1] if len(r["dates"]) > 1 else "",
        })
    return country, section, out


FIELDS = ["approval_number", "name", "address", "city", "postcode", "region",
          "country", "group", "section", "activity", "slaughter", "species",
          "remarks", "date_of_approval", "publication_date"]


def main():
    if not SRC.exists():
        sys.exit(f"create {SRC}/ and drop your pasted .txt files in it")
    DST.mkdir(parents=True, exist_ok=True)

    total, files = 0, 0
    for path in sorted(SRC.glob("*.txt")):
        if path.name.startswith("_"):
            continue          # notes and instructions, not pasted listings
        text = path.read_text(encoding="utf-8", errors="replace")
        for doc in split_documents(text):
            country, section, rows = parse_document(doc, fallback_country=path.stem)
            if not rows:
                print(f"  !! {path.name}: no establishments parsed. If the file "
                      f"looks right, its tabs were probably converted to spaces "
                      f"or the copy is incomplete.", file=sys.stderr)
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", (country or path.stem).lower()).strip("-")
            dest = DST / f"{slug}.csv"

            existing = []
            if dest.exists():
                with open(dest, encoding="utf-8-sig", newline="") as fh:
                    existing = list(csv.DictReader(fh))
            # The key must include the section. One plant is commonly listed
            # under several commodity codes -- a works appears under RM and PM
            # and ABP-SH with the same national number -- and each listing
            # carries its own activity codes and species. Keying on number and
            # name alone silently drops every listing after the first.
            key = lambda r: (r.get("approval_number"), r.get("name"),
                             r.get("section"))
            seen = {key(r) for r in existing}
            merged = existing + [r for r in rows if key(r) not in seen]

            with open(dest, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(merged)

            new = len(merged) - len(existing)
            sh = sum(1 for r in rows if r["slaughter"] == "Y")
            unk = sum(1 for r in rows if r["slaughter"] == "")
            nospec = sum(1 for r in rows if not r["species"])
            warn = ("" if section in MEAT_SECTIONS or section in NO_ACTIVITY_VOCAB
                    else "  <- not a meat or ABP-SH section")
            grp = group_for(country)
            print(f"  {country or path.stem:22} {grp:13} {section or '?':5} "
                  f"{len(rows):4} rows, {sh:4} slaughter"
                  f"{f', {unk} unstated' if unk else ''}, {new:4} new"
                  f" ({nospec} without species) -> {dest.name}{warn}")
            total += new
            files += 1

    print(f"\n{total:,} new establishments across {files} listings")
    if total:
        print("next: commit raw/eu_traces_third_country/, then run workflow 3")


if __name__ == "__main__":
    main()
