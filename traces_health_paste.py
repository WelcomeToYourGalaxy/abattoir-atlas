"""
TRACES animal-health establishment listings, from a copied page.

A second register, separate from the food-hygiene one `traces_paste.py` reads.
That one lists plants approved to handle meat; this one lists premises approved
to hold live animals -- assembly centres, poultry establishments, hatcheries,
quarantine stations -- under Regulation (EU) 2019/2035. No premises in it kills
animals, which is the point of carrying it: the food register shows where the
killing happens, this one shows the confinement and movement that feeds it.

The page layout differs enough to need its own reader. The food listing prints
one establishment per block with the fields on separate lines; this one prints a
pipe-delimited table, with extra species running onto continuation lines:

    Approval number|Name |Street|City|Region|Activities|Remarks|
    ES150780206801|CONCELLO SANTIAGO|MERCADO DE GANDO|15703 Santiago|...
            C Capra hircus
            O Ovis aries|

One paste can hold many country/section blocks back to back, so the country and
the section are read from the block headers rather than from a filename.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

# Section code and label, from the "Section:" line. The register groups its
# sections under headings that are not always "Animal" -- the germinal-product
# sections read "Germinal products EMB-COL Embryo collection teams" -- so the
# heading is skipped and the code is taken as the first all-caps token.
SECTION_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z ]*?\s)?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s+(\S.*)$")

# "39649 Villacarriedo", "BT35 Armagh", "15703 Santiago de Compostela".
# A leading token carrying a digit is the postcode; the rest is the town.
POSTCODE_RE = re.compile(r"^([A-Z]{0,2}[\d][\w\-]*(?:\s+\d[\w\-]*)?)\s+(.+)$")

# The species vocabulary the remarks column uses, same as the food register.
SPECIES_RE = re.compile(
    r"^([BCOSP])\s+(Bovinae|Capra hircus|Ovis aries|Equidae|Suidae)\s*$")
SPECIES_MAP = {"Bovinae": "bovine", "Suidae": "porcine", "Ovis aries": "ovine",
               "Capra hircus": "caprine", "Equidae": "equine"}

FIELDS = ["approval_number", "name", "address", "city", "postcode", "region",
          "country", "section", "section_label", "activity", "species",
          "last_update"]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip(" \t•|")


def _split_city(raw: str) -> tuple[str, str]:
    """(city, postcode). Returns the whole string as the city when no leading
    postcode is present rather than inventing one."""
    c = _clean(raw)
    m = POSTCODE_RE.match(c)
    return (m.group(2).strip(), m.group(1).strip()) if m else (c, "")


# Page furniture that follows the last row of a block when the copy runs to the
# bottom of the page. Without this the final establishment in every block ends
# up with the cookie notice recorded as one of its activities.
CHROME_RE = re.compile(
    r"(Last update:|Legal Notice|Terms of Use|Privacy Statement|Accessibility"
    r"|Top Page|Skip to Main Content|Documentation\(|IMSOC)", re.I)


def _bullets(cell: str) -> list:
    """The activities and remarks cells hold bullet lists flattened into tabs."""
    out = []
    for piece in (cell or "").split("\t"):
        m = CHROME_RE.search(piece)
        if m:
            piece = piece[:m.start()]
        v = _clean(piece)
        if v:
            out.append(v)
    return out


def parse(text: str) -> list:
    """Every establishment in a paste, across all country/section blocks."""
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    rows, country, section, label, updated = [], "", "", "", ""
    cur = None
    in_table = False

    for i, raw in enumerate(lines):
        line = raw.replace("\xa0", " ").rstrip()
        stripped = line.strip()

        if stripped == "Country:":
            country = _clean(lines[i + 1]) if i + 1 < len(lines) else ""
            in_table = False
            continue
        if stripped == "Section:":
            m = SECTION_RE.match(_clean(lines[i + 1]) if i + 1 < len(lines) else "")
            section, label = (m.group(1), m.group(2)) if m else ("", "")
            continue
        if stripped == "Last update:":
            updated = _clean(lines[i + 1]) if i + 1 < len(lines) else ""
            continue
        if stripped.startswith("Approval number|"):
            in_table = True
            continue
        if in_table and CHROME_RE.search(stripped) and "|" not in stripped:
            in_table = False
            continue
        if not in_table or not stripped:
            continue

        # A continuation line carries no approval number: it belongs to the row
        # above. Losing these loses the second and third species of a listing.
        if line.startswith(("\t", " ")) and "|" not in stripped.split("\t")[0]:
            if cur is not None:
                for b in _bullets(line):
                    m = SPECIES_RE.match(b)
                    if m:
                        cur["species"].append(SPECIES_MAP[m.group(2)])
                    elif b not in cur["activity"]:
                        cur["activity"].append(b)
            continue

        if "|" not in line:
            in_table = False          # page chrome after the table
            continue

        f = line.split("|")
        if len(f) < 6 or not _clean(f[0]):
            continue
        if cur:
            rows.append(cur)
        city, postcode = _split_city(f[3] if len(f) > 3 else "")
        cur = {
            "approval_number": _clean(f[0]),
            "name": _clean(f[1]) if len(f) > 1 else "",
            "address": _clean(f[2]) if len(f) > 2 else "",
            "city": city,
            "postcode": postcode,
            "region": _clean(f[4]) if len(f) > 4 else "",
            "country": country,
            "section": section,
            "section_label": label,
            "activity": _bullets(f[5]) if len(f) > 5 else [],
            "species": [],
            "last_update": updated,
        }
        for b in (_bullets(f[6]) if len(f) > 6 else []):
            m = SPECIES_RE.match(b)
            if m:
                cur["species"].append(SPECIES_MAP[m.group(2)])
    if cur:
        rows.append(cur)
    return rows


def write_csv(rows: list, out_path: str) -> int:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({
                **{k: r[k] for k in FIELDS if k not in ("activity", "species")},
                "activity": " | ".join(dict.fromkeys(r["activity"])),
                "species": " ".join(dict.fromkeys(r["species"])),
            })
    return len(rows)


def main(argv):
    if len(argv) < 3:
        print("usage: python traces_health_paste.py <paste.txt> <out.csv>")
        return 1
    text = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    rows = parse(text)
    n = write_csv(rows, argv[2])
    print(f"{n:,} establishments -> {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
