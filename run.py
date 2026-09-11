#!/usr/bin/env python3
"""
Pipeline driver.

  python run.py parse      --source us_fsis_mpi
  python run.py dedup
  python run.py geocode    --contact you@example.org [--limit 2000]
  python run.py build
  python run.py selftest

Stages write to work/ and read from work/, so any stage can be re-run alone.
Fetching is deliberately not automated for the registries that publish behind a
changing date-stamped URL or a query form: download those by hand into raw/ and
record the date, which keeps the snapshot honest.
"""

from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import build_map
import dedup as dedup_mod
import parsers
from schema import SourceRecord

ROOT = Path(__file__).parent
RAW, WORK, OUT = ROOT / "raw", ROOT / "work", ROOT / "out"
for d in (RAW, WORK, OUT):
    d.mkdir(exist_ok=True)

RECORDS = WORK / "records.pkl"
FACILITIES = WORK / "facilities.pkl"

SOURCE_LABELS = {
    "us_fsis_mpi": {"name": "USDA FSIS inspection directory", "short": "USDA FSIS"},
    "eu_traces_third_country": {"name": "EU authorised establishments, non-EU countries",
                                "short": "EU third-country list"},
    "eu_member_states": {"name": "EU member state approved establishments",
                         "short": "EU member state"},
    "uk_fsa": {"name": "UK FSA approved establishments", "short": "UK FSA"},
    "cifer_china": {"name": "China GACC import food enterprise registration",
                    "short": "China CIFER"},
    "ca_cfia": {"name": "CFIA licensed establishments", "short": "Canada CFIA"},
    "br_sif": {"name": "Serviço de Inspeção Federal", "short": "Brazil SIF"},
    "au_daff": {"name": "Australian export-registered establishments", "short": "Australia DAFF"},
    "nz_mpi": {"name": "NZ registered risk management programmes", "short": "NZ MPI"},
    "osm_overpass": {"name": "OpenStreetMap", "short": "OpenStreetMap"},
}


def _load(path: Path):
    if not path.exists():
        sys.exit(f"missing {path} — run the earlier stage first")
    with open(path, "rb") as fh:
        return pickle.load(fh)


def _save(obj, path: Path):
    with open(path, "wb") as fh:
        pickle.dump(obj, fh)


# ---------------------------------------------------------------- parse

def cmd_parse(args):
    existing = _load(RECORDS) if RECORDS.exists() else []
    existing = [r for r in existing if r.source_id != args.source]

    sid = args.source
    if sid == "us_fsis_mpi":
        new = parsers.parse_fsis(
            RAW / (args.file or "fsis_mpi_directory.csv"),
            RAW / (args.demographic or "fsis_demographic.csv"),
            snapshot=args.snapshot,
        )
    elif sid in ("eu_traces_third_country", "eu_member_states", "uk_fsa"):
        new = []
        for f in sorted((RAW / sid).glob("*.csv")):
            new += parsers.parse_eu_list(
                f, source_id=sid,
                id_scheme={"uk_fsa": "UK-APPROVAL"}.get(sid, "EU-APPROVAL"),
                country_iso3=args.country, snapshot=args.snapshot)
    elif sid == "cifer_china":
        new = parsers.parse_cifer(RAW / (args.file or "cifer.jsonl"), args.snapshot)
    elif sid == "osm_overpass":
        new = parsers.parse_osm(RAW / (args.file or "osm.json"), args.snapshot)
    else:
        if not args.country:
            sys.exit(f"{sid} uses the generic parser — pass --country ISO3")
        new = parsers.parse_generic(
            RAW / args.file, source_id=sid,
            id_scheme=args.id_scheme or sid.upper(),
            country_iso3=args.country, snapshot=args.snapshot,
            slaughter_default=None if args.slaughter is None else bool(args.slaughter))

    _save(existing + new, RECORDS)
    print(f"{sid}: {len(new):,} records parsed "
          f"({sum(1 for r in new if r.slaughter is True):,} flagged slaughter, "
          f"{sum(1 for r in new if r.src_lat is not None):,} with source coordinates)")
    print(f"total in store: {len(existing) + len(new):,}")


# ---------------------------------------------------------------- dedup

def cmd_dedup(args):
    records = _load(RECORDS)
    geocache = {}
    if (WORK / "geo.json").exists():
        geocache = json.loads((WORK / "geo.json").read_text())

    facilities, review = dedup_mod.cluster(records, geocache)
    _save(facilities, FACILITIES)
    dedup_mod.write_review_queue(review, str(OUT / "review_queue.csv"))

    per_source = Counter(r.source_id for r in records)
    tiers = Counter(t for f in facilities for t in f.match_tiers)
    sizes = Counter(len(f.members) for f in facilities)

    lines = [
        "# Deduplication report", "",
        f"Generated {date.today().isoformat()}", "",
        f"- Input rows: **{len(records):,}**",
        f"- Facilities after clustering: **{len(facilities):,}**",
        f"- Rows absorbed into a multi-source facility: "
        f"**{len(records) - len(facilities):,}**",
        f"- Flagged for review, not merged: **{len(review):,} pairs**", "",
        "## Rows per source", "",
    ]
    lines += [f"- {SOURCE_LABELS.get(s,{}).get('short',s)}: {n:,}"
              for s, n in per_source.most_common()]
    lines += ["", "## What joined the merges", ""]
    lines += [f"- `{t}`: {n:,} clusters" for t, n in tiers.most_common()]
    lines += ["", "## Cluster sizes", ""]
    lines += [f"- {k} source record{'s' if k>1 else ''}: {v:,} facilities"
              for k, v in sorted(sizes.items())]
    lines += ["", "Nothing was discarded. Every input row is inside exactly one "
              "facility above, and `out/facilities.json` carries all of them "
              "verbatim.", ""]
    (OUT / "dedup_report.md").write_text("\n".join(lines), encoding="utf-8")

    # Gzipped: at full scale the plain file runs past GitHub's 100 MB per-file
    # limit, and this is the artifact that has to live in the repo.
    blob = json.dumps([f.to_dict() for f in facilities], ensure_ascii=False)
    with gzip.open(OUT / "facilities.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(blob)
    print(f"facilities.json.gz: {(OUT / 'facilities.json.gz').stat().st_size/1e6:.1f} MB "
          f"({len(blob)/1e6:.1f} MB uncompressed)")

    print("\n".join(lines[:12]))
    print(f"\nwrote out/facilities.json.gz, out/dedup_report.md, "
          f"out/review_queue.csv ({len(review):,} pairs)")


# ---------------------------------------------------------------- geocode

def cmd_geocode(args):
    import geocode as geo
    records = _load(RECORDS)
    cache = {}
    if (WORK / "geo.json").exists():
        cache = json.loads((WORK / "geo.json").read_text())
    fresh = geo.geocode_records(records, contact=args.contact, limit=args.limit)
    cache.update(fresh)
    (WORK / "geo.json").write_text(json.dumps(cache), encoding="utf-8")
    prec = Counter(v["precision"] for v in cache.values())
    print(f"geocache now holds {len(cache):,} entries")
    for p, n in prec.most_common():
        print(f"  {p}: {n:,}")
    have, left = geo.pending_count(records)
    print(f"\naddresses cached: {have:,}   still to look up: {left:,}")
    (WORK / "geocode_pending").write_text(str(left))
    if left:
        print("run this stage again to continue; the cache carries over.")
    else:
        print("done. re-run `dedup` so the new coordinates feed the geo match "
              "tier, then `build`.")


# ---------------------------------------------------------------- build

def cmd_build(args):
    facilities = _load(FACILITIES)
    report = build_map.build(
        facilities, str(OUT / (args.out or "abattoir_atlas.html")),
        title=args.title, subtitle=args.subtitle,
        sources_meta=SOURCE_LABELS)
    print(json.dumps(report, indent=2))
    if report["warning"]:
        print("\n" + report["warning"])


# ---------------------------------------------------------------- selftest

def cmd_selftest(args):
    """End-to-end run on synthetic rows, to prove the wiring before real data.

    The fixtures below are invented. They exist to exercise the matcher -- one
    plant listed by three registries under the same USDA number, one pair that
    should go to review rather than merge, one unlocatable record.
    """
    snap = "2026-09-10"
    recs = [
        SourceRecord("us_fsis_mpi", snap, "M1234", "Heartland Beef Processing LLC",
                     "USA", "M1234", "US-FSIS", address="4400 Stockyard Rd",
                     locality="Dodge City", admin1="KS", postcode="67801",
                     species=["bovine"], activities=["slaughter"], slaughter=True,
                     src_lat=37.7528, src_lon=-100.0171),
        SourceRecord("eu_traces_third_country", snap, "US-1234",
                     "HEARTLAND BEEF PROCESSING", "USA", "M1234", "US-FSIS",
                     address="4400 Stockyard Road, Dodge City KS",
                     species=["bovine"], activities=["slaughter"], slaughter=True),
        SourceRecord("cifer_china", snap, "CNUS0099", "Heartland Beef Processing",
                     "USA", "CNUS0099", "CN-CIFER", foreign_id="M1234",
                     foreign_id_scheme="US-FSIS", species=["bovine"],
                     activities=["unknown"], slaughter=None),

        SourceRecord("us_fsis_mpi", snap, "P5501", "Valley Poultry Co", "USA",
                     "P5501", "US-FSIS", address="12 Mill Lane",
                     locality="Gainesville", admin1="GA", postcode="30501",
                     species=["poultry"], activities=["slaughter"], slaughter=True,
                     src_lat=34.2979, src_lon=-83.8241),
        SourceRecord("osm_overpass", snap, "way/887711", "Valley Poultry", "USA",
                     activities=["slaughter"], slaughter=True,
                     src_lat=34.2981, src_lon=-83.8238),

        # Same company name, two different plants, no usable location: review.
        SourceRecord("eu_traces_third_country", snap, "BR-77a", "Frigorifico Boa Vista",
                     "BRA", "77a", "EU-THIRDCOUNTRY", locality="Goiânia",
                     species=["bovine"], activities=["slaughter"], slaughter=True),
        SourceRecord("eu_traces_third_country", snap, "BR-92b", "Frigorifico Boa Vista",
                     "BRA", "92b", "EU-THIRDCOUNTRY", locality="Cuiabá",
                     species=["bovine"], activities=["slaughter"], slaughter=True),

        SourceRecord("eu_member_states", snap, "ES-10.0412", "Mataderos del Ebro SL",
                     "ESP", "10.0412", "EU-APPROVAL", address="Pol. Ind. La Vega 7",
                     locality="Zaragoza", postcode="50015", species=["porcine"],
                     activities=["slaughter", "cutting"], slaughter=True,
                     src_lat=41.6795, src_lon=-0.8710),
    ]
    _save(recs, RECORDS)
    facilities, review = dedup_mod.cluster(recs, {})
    _save(facilities, FACILITIES)

    assert len(recs) == 8, "fixture count changed"
    assert len(facilities) == 5, f"expected 5 clusters, got {len(facilities)}"
    big = max(facilities, key=lambda f: len(f.members))
    assert len(big.members) == 3 and "id" in big.match_tiers, \
        "three-registry plant should join on the establishment number"
    assert any(len(f.members) == 2 and "geo" in f.match_tiers for f in facilities), \
        "OSM point should join the FSIS record on proximity"
    assert len(review) >= 1, "identical Brazilian names should reach the review queue"
    assert all(f.review is False or f.country_iso3 == "BRA" for f in facilities)

    dedup_mod.write_review_queue(review, str(OUT / "review_queue.csv"))
    rep = build_map.build(
        facilities, str(OUT / "smoketest_synthetic.html"),
        title="Smoke test — synthetic data, not real facilities",
        subtitle="Eight invented rows used to exercise the matcher and the "
                 "renderer. No record here corresponds to a real plant.",
        sources_meta=SOURCE_LABELS)

    print(f"pass — {len(recs)} rows to {len(facilities)} facilities, "
          f"{len(review)} review pairs")
    for f in sorted(facilities, key=lambda x: -len(x.members)):
        print(f"  {f.name[:42]:44} {len(f.members)} src  "
              f"tiers={f.match_tiers or ['-']}  "
              f"{'mapped' if f.mappable else 'unlocated'}")
    print(f"\nwrote {rep['path']} ({rep['size_mb']} MB)")


# ---------------------------------------------------------------- cli

def cmd_status(args):
    import geocode as geo
    if not RECORDS.exists():
        print("no records parsed yet"); return
    records = _load(RECORDS)
    per = Counter(r.source_id for r in records)
    have, left = geo.pending_count(records)
    print(f"source records: {len(records):,}")
    for s, n in per.most_common():
        print(f"  {SOURCE_LABELS.get(s,{}).get('short',s)}: {n:,}")
    print(f"geocode cache: {have:,} resolved, {left:,} outstanding")
    if FACILITIES.exists():
        fac = _load(FACILITIES)
        mapped = sum(1 for f in fac if f.mappable)
        print(f"facilities: {len(fac):,} ({mapped:,} mappable, "
              f"{len(fac)-mapped:,} unlocated)")
    else:
        print("facilities: not clustered yet")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("parse"); a.set_defaults(fn=cmd_parse)
    a.add_argument("--source", required=True)
    a.add_argument("--file"); a.add_argument("--demographic")
    a.add_argument("--country"); a.add_argument("--id-scheme")
    a.add_argument("--slaughter", type=int, choices=[0, 1], default=None)
    a.add_argument("--snapshot", default=None)

    b = sub.add_parser("dedup"); b.set_defaults(fn=cmd_dedup)

    c = sub.add_parser("geocode"); c.set_defaults(fn=cmd_geocode)
    c.add_argument("--contact", required=True,
                   help="email or URL for the geocoder User-Agent")
    c.add_argument("--limit", type=int, default=None,
                   help="cap fresh lookups this run; cache persists between runs")

    d = sub.add_parser("build"); d.set_defaults(fn=cmd_build)
    d.add_argument("--out")
    d.add_argument("--title", default="Where animals are killed")
    d.add_argument("--subtitle",
                   default="Industrial slaughter facilities compiled from national "
                           "inspection registries and the EU and Chinese export "
                           "approval lists.")

    e = sub.add_parser("selftest"); e.set_defaults(fn=cmd_selftest)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
