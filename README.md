# Global abattoir atlas — build pipeline

Compiles slaughter facilities from national inspection registries and the EU and
Chinese export-approval lists into one deduplicated dataset and a single-file
Leaflet map.

**Running this on GitHub Actions instead of a terminal: see REPO-SETUP.md.**
That covers the repo layout, the three settings to change, and the run order.

## Why some fetching is manual

The sandbox this was written in has no route to `fsis.usda.gov`,
`food.ec.europa.eu`, `ciferquery.singlewindow.cn` or `industry.eea.europa.eu`, so
no registry data was downloaded and none is bundled. Everything here is the
machinery.

`fetch.py` handles what can be automated: it reads the current CSV links off the
FSIS landing page (they carry a weekly date stamp, so they can't be hard-coded)
and runs the global Overpass query. `python fetch.py list` prints what still
needs a human — the EU per-country lists, which have a different layout per
member state, and CIFER, which publishes no bulk export.

## Run order

```bash
python run.py selftest                    # verify the wiring first
python fetch.py all                       # FSIS + OpenStreetMap into raw/

# upload the EU lists into raw/ by hand, then:
python run.py parse --source us_fsis_mpi \
    --file fsis_mpi_directory.csv --demographic fsis_demographic.csv --snapshot 2026-09-10
python run.py parse --source eu_traces_third_country   # reads raw/eu_traces_third_country/*.csv
python run.py parse --source cifer_china --file cifer.jsonl
python run.py parse --source osm_overpass --file osm.json
# or just: bash parse_all.sh

python run.py dedup                       # first pass, ID and address tiers only
python run.py geocode --contact you@welcometoyourgalaxy.com --limit 5000
python run.py dedup                       # second pass, now with the geo tier live
python run.py build
```

`geocode` and `dedup` are meant to alternate. The first dedup pass collapses
everything joinable by establishment number, which cuts the geocoding bill
before you spend it; the second pass uses the resulting coordinates to catch
duplicates that share no ID.

Outputs land in `out/`:

| file | what it is |
|---|---|
| `abattoir_atlas.html` | the map, one file |
| `facilities.json.gz` | every facility with all of its source records verbatim |
| `review_queue.csv` | pairs the matcher would not decide — fill in the `decision` column |
| `dedup_report.md` | counts per source, per match tier, per cluster size |

## How deduplication behaves

Three tiers merge, in descending strength:

- **id** — the same establishment number in the same scheme. This does most of
  the work, because a plant exporting to the EU and to China appears in three
  registries all quoting the number its home authority issued. CIFER records
  carry that foreign number explicitly, which is what makes the join possible.
- **addr** — same country, same postcode, same street number, name token overlap
  ≥ 0.60.
- **geo** — coordinates within 250 m, name token overlap ≥ 0.60.

Anything weaker goes to `review_queue.csv` as a pair and stays unmerged. In
particular, identical company names inside one country with no usable address are
never merged — one operator running six plants under one name is normal in this
industry, and auto-merging those would delete five real facilities.

Nothing is discarded at any point. A merge is a grouping. Every input row sits
inside exactly one facility in `facilities.json`, with its own name, number and
address as its registry printed them, so any merge can be read back and undone.

Thresholds are at the top of `dedup.py`. They are set conservatively on the view
that an unmerged duplicate is a visible error you can fix, while a wrong merge
silently destroys a distinct plant.

## How locations behave

A failed geocode produces no coordinate. There is no fallback to a town centroid
and none to a country centroid. Records that cannot be placed to a street or a
building are counted, listed in the map's left rail, and exported in full — they
are not scattered across the map as if they were known.

`geo_precision` travels with every coordinate. Only `rooftop` and `street` are
drawn.

## Rendering

No clustering. Every facility is its own point at every zoom, so the world view
shows the real distribution: where plants crowd together the dots overlap and
the tone deepens. Nothing is hidden inside a count bubble.

That works at scale because the projection is done once at load into
Float64Arrays, points are pre-grouped by colour so `fillStyle` is set three or
four times a frame instead of 60,000, and the filter result lives in a
Uint8Array rebuilt only when a filter changes — panning never re-evaluates a
filter. Below zoom 8 dots draw as `fillRect`; above it as one batched path with
a single fill.

Measured at 60,000 points: 1.5 ms per frame of projection arithmetic at world
zoom with 43,000 points in view, and 0.5 ms per hit-test query. Hit testing is a
throttled linear scan, so there is no spatial index to keep in sync.

Where several plants overlap at a given zoom, clicking opens a list of them by
name rather than a count — they are separate facilities and stay that way.

## Scale

Rehearsed at 80,000 input rows: clustering runs in about 4 seconds, the built
map is 6.7 MB, 1.7 MB gzipped, and holds 60,000 facilities. Leaflet loads from
unpkg; everything else, data included, is in the file.

If a host chokes on the payload, split it rather than trimming the data.

## Known gaps, carried deliberately

- Export registries list plants cleared to ship abroad. Domestic-only and
  small-throughput facilities are absent, and in most countries those are the
  majority by count. The atlas will undercount, unevenly by country, and the
  shape of that undercount is itself worth stating on the published page.
- Brazil's SIF list is federal inspection only; state (SIE) and municipal (SIM)
  plants are separate registries. Australia's DAFF list is export-registered
  only; domestic abattoirs are licensed by the states.
- `sources.yml` marks each source `verified` or `needs_check`. The ones marked
  `needs_check` are real registries whose current download URL was not
  confirmed. Resolve those before the first run rather than trusting the string.
- OpenStreetMap is ODbL. If OSM records reach the published map, the attribution
  in the basemap line has to stay.

## Files

```
schema.py      canonical record + facility shape, controlled vocabularies
normalize.py   name/address/ID normalization used only for comparison
parsers.py     per-source readers; resolve columns by name, fail loudly on drift
dedup.py       union-find matcher, review queue, merge attribution
geocode.py     geocoding, cached to a portable geocache.json.gz
fetch.py       downloads the sources that can be automated
cifer.py       China GACC/CIFER retrieval — run `python cifer.py discover` first
build_map.py   compact encoding + the single-file Leaflet atlas
run.py         CLI: parse / dedup / geocode / build / status / selftest
parse_all.sh   parses whatever is present in raw/; used by the workflows
sources.yml    registry of sources, URLs, verification status, coverage bias
```
