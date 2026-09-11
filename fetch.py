#!/usr/bin/env python3
"""
Fetchers for the sources a runner can pull on its own.

FSIS and Overpass are fully automatic. The rest are listed here with what they
need, because pretending to automate a download I could not test would just
produce a workflow that fails silently at 3am.

    python fetch.py fsis
    python fetch.py osm
    python fetch.py all          # everything automatic; skips the rest with a note
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

RAW = Path("raw")
UA = {"User-Agent": "abattoir-atlas/1.0 (+https://welcometoyourgalaxy.com)"}


def _get(url: str, *, data: bytes | None = None, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, data=data, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            if attempt == 3:
                raise
            wait = 2 ** attempt * 3
            print(f"  retry after {exc}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------

FSIS_PAGE = ("https://www.fsis.usda.gov/inspection/establishments/"
             "meat-poultry-and-egg-product-inspection-directory")


def fetch_fsis() -> None:
    """FSIS date-stamps its CSV filenames and changes them weekly, so the links
    are read off the landing page rather than hard-coded. If FSIS restructures
    the page this fails loudly with the links it did find, which is the
    behaviour you want at that point."""
    RAW.mkdir(exist_ok=True)
    print(f"reading {FSIS_PAGE}")
    html = _get(FSIS_PAGE).decode("utf-8", "replace")

    links = {urllib.parse.urljoin(FSIS_PAGE, m)
             for m in re.findall(r'href="([^"]+\.csv[^"]*)"', html, re.I)}
    if not links:
        sys.exit("no .csv links on the FSIS page — it has been restructured; "
                 "download by hand into raw/ and skip this step")

    def pick(*words):
        for url in sorted(links):
            low = url.lower()
            if all(w in low for w in words):
                return url
        return None

    targets = [
        (pick("mpi", "establishment", "number") or pick("mpi", "number")
         or pick("directory"), "fsis_mpi_directory.csv"),
        (pick("demographic"), "fsis_demographic.csv"),
    ]

    for url, dest in targets:
        if not url:
            print(f"  could not identify {dest}. Links found:")
            for l in sorted(links):
                print(f"    {l}")
            sys.exit(1)
        print(f"  {dest} <- {url}")
        (RAW / dest).write_bytes(_get(url))
        print(f"    {(RAW / dest).stat().st_size/1e6:.1f} MB")


# ---------------------------------------------------------------------------

OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = """
[out:json][timeout:900];
(
  nwr["industrial"="slaughterhouse"];
  nwr["man_made"="works"]["works"="slaughterhouse"];
  nwr["craft"="slaughterhouse"];
);
out center tags;
"""


def fetch_osm() -> None:
    """A global Overpass query. It is a heavy one and the public instance may
    refuse it at busy times -- that is a queue, not an error, so retry later
    rather than narrowing the query."""
    RAW.mkdir(exist_ok=True)
    print("querying Overpass (global, expect several minutes)")
    body = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode()
    out = _get(OVERPASS, data=body, timeout=1200)
    (RAW / "osm.json").write_bytes(out)
    n = out.count(b'"type"')
    print(f"  raw/osm.json  {len(out)/1e6:.1f} MB, roughly {n:,} elements")


# ---------------------------------------------------------------------------

MANUAL = {
    "eu_traces_third_country": (
        "EU authorised establishments in non-EU countries",
        "https://food.ec.europa.eu/food-safety/biological-safety/food-hygiene/"
        "non-eu-countries-authorised-establishments_en",
        "Per-country files behind an index. Download the meat sections "
        "(Annex III Sections I-IV), convert each to CSV, and put them in "
        "raw/eu_traces_third_country/."),
    "eu_member_states": (
        "EU member state approved establishments",
        "https://www.fsai.ie/enforcement-and-legislation/official-controls/"
        "mancp/approved-food-premises",
        "One list per member state, one layout per member state. Convert to "
        "CSV into raw/eu_member_states/. Start with the large producers: "
        "DE, FR, ES, PL, IT, NL, DK."),
    "cifer_china": (
        "China GACC CIFER",
        "https://ciferquery.singlewindow.cn",
        "No bulk export. Run `python cifer.py discover` locally once to "
        "capture the request shape, commit the edited cifer.py, then the "
        "harvest workflow can run it unattended."),
}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=["fsis", "osm", "all", "list"])
    a = p.parse_args()

    if a.what == "list":
        for k, (name, url, note) in MANUAL.items():
            print(f"\n{name}\n  {url}\n  {note}")
        return

    if a.what in ("fsis", "all"):
        fetch_fsis()
    if a.what in ("osm", "all"):
        fetch_osm()
    if a.what == "all":
        print("\nAutomatic sources done. Still needs a human:")
        for k, (name, url, note) in MANUAL.items():
            print(f"  - {name}: {note.splitlines()[0]}")


if __name__ == "__main__":
    main()
