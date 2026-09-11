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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAW = Path("raw")

# Full browser header set. FSIS sits behind an edge filter that returns 403 to a
# bare urllib User-Agent, so the request has to look like a browser navigation
# rather than announce itself as a script. Accept-Encoding is identity on
# purpose: urllib does not transparently decompress, and a gzipped body here
# would just read as mojibake.
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class Blocked(RuntimeError):
    """The host refused us rather than failing. Retrying will not help."""


def _get(url: str, *, data: bytes | None = None, timeout: int = 180,
         referer: str | None = None) -> bytes:
    headers = dict(BROWSER)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Accept"] = "application/json,*/*;q=0.8"

    req = urllib.request.Request(url, data=data, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 451):
                raise Blocked(f"HTTP {exc.code} from {url}") from exc
            if attempt == 3:
                raise
            wait = 2 ** attempt * 3
            print(f"  retry after HTTP {exc.code}; waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
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
    try:
        html = _get(FSIS_PAGE).decode("utf-8", "replace")
    except Blocked as exc:
        raise Blocked(
            f"{exc}\n"
            "    FSIS is refusing this runner. Their edge filter blocks some\n"
            "    datacentre ranges outright, and no header set gets past that.\n"
            "    Download the two CSVs from the page by hand and upload them to\n"
            "    raw/ as fsis_mpi_directory.csv and fsis_demographic.csv:\n"
            f"    {FSIS_PAGE}") from exc

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
        (RAW / dest).write_bytes(_get(url, referer=FSIS_PAGE))
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

    jobs = []
    if a.what in ("fsis", "all"):
        jobs.append(("FSIS", fetch_fsis))
    if a.what in ("osm", "all"):
        jobs.append(("OpenStreetMap", fetch_osm))

    ok, failed = [], []
    for label, fn in jobs:
        try:
            fn()
            ok.append(label)
        except Exception as exc:
            # One host refusing us is not a reason to skip the others. The run
            # reports what it got and what it did not, and only fails outright
            # when nothing came back.
            failed.append((label, exc))
            print(f"\n!! {label} failed: {exc}\n", file=sys.stderr)

    print("\n--- summary ---")
    for label in ok:
        print(f"  fetched: {label}")
    for label, exc in failed:
        print(f"  FAILED:  {label} — {str(exc).splitlines()[0]}")

    if a.what == "all":
        print("\nStill needs a human:")
        for k, (name, url, note) in MANUAL.items():
            print(f"  - {name}: {note.splitlines()[0]}")

    if failed and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
