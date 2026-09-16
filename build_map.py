"""
Builds the atlas as one HTML file.

Every facility is drawn as its own point at every zoom level. Nothing is rolled
into a count bubble, so what you see at world view is the real distribution:
where plants crowd together the dots overlap and the tone deepens, and that
density is a property of the data rather than a number drawn on top of it.

How that stays fast at 60,000+ points:

  * Mercator position is computed once at load into Float64Arrays. Per frame the
    work is two multiplies and a subtract per point, not a Leaflet projection
    call that allocates an object each time.
  * Points are pre-grouped by colour, so the canvas sets fillStyle a handful of
    times per frame instead of once per point.
  * The filter result lives in a Uint8Array rebuilt only when a filter changes.
    Panning never re-evaluates a filter.
  * Below zoom 8 points draw as fillRect, which is several times cheaper than a
    path arc. Above it they draw as one batched path with a single fill.

Encoding notes:

  * Positions are integers, degrees x 1e5, in flat parallel arrays rather than
    GeoJSON. At 70,000 facilities that is roughly a fifth of the bytes.
  * Repeated strings -- country, species combination, source combination -- are
    dictionary-encoded once and referenced by index.
  * Full provenance stays in facilities.json. The map embeds enough for the
    drawer to show which registries listed a plant and under what number.

Nothing is dropped for size. If the output is too heavy for a given host, the
builder says so in the report -- that is a hosting decision for you, not a
silent filter.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from datetime import date
from pathlib import Path

SPECIES_COLOUR = {
    "bovine": "#a4635c",
    "porcine": "#a9766a",
    "poultry": "#7e8e6c",
    "ovine": "#6d8b93",
    "caprine": "#6d8b93",
    "equine": "#8b8474",
    "cervid": "#8b8474",
    "lagomorph": "#8b8474",
    "farmed_game": "#8b8474",
    "wild_game": "#8b8474",
    "other": "#8b8474",
    "fish": "#6d8b93",
    "crustacean": "#6d8b93",
    "camelid": "#8b8474",
    "canine": "#8b8474",
    "mustelid": "#8b8474",
    "reptile": "#8b8474",
    "insect": "#7e8e6c",
    "_mixed": "#8f8d7e",
    "_unstated": "#5e6a66",
}

# The palette above is built for a flat dark field: mid-tone, low-chroma, so a
# dense cluster reads as depth rather than glare. Over satellite imagery every
# one of those colours lands on ground of roughly its own value and disappears.
# This is the same hue for each species, lifted well above the imagery in
# lightness, and it is only used while the imagery basemap is on.
# Over the painted atlas and over imagery the earth palette above turns to mud:
# every hue in it is a shade of the ground it is sitting on. These are spread
# around the wheel instead, so a species is told apart by hue rather than by a
# difference in brown, and each one is light enough to hold against dark
# woodland and saturated enough to hold against a pale field.
# Red through green to blue, with nothing in the magenta-violet quarter of the
# wheel. Every entry is light enough to hold against dark woodland and strong
# enough to hold against a pale field.
SPECIES_COLOUR_SATELLITE = {
    "bovine": "#FF6B5A",       # red
    "porcine": "#FF9A6B",      # coral
    "poultry": "#FFD84D",      # yellow
    "ovine": "#3FD9F5",        # cyan
    "caprine": "#5FE87F",      # green
    "equine": "#FFB13D",       # amber
    "cervid": "#BEF04A",       # lime
    "lagomorph": "#FF8F7A",    # salmon
    "farmed_game": "#4FE8C6",  # aqua
    "wild_game": "#5AAEFF",    # blue
    "other": "#F0F0F0",        # white
    "fish": "#35CDFF",         # sky
    "crustacean": "#57F0DC",   # turquoise
    "camelid": "#FFC98A",      # sand
    "canine": "#FFC233",       # gold
    "mustelid": "#A8E85C",     # yellow-green
    "reptile": "#74F0B8",      # mint
    "insect": "#D6F05C",       # chartreuse
    "_mixed": "#FFFFFF",       # more than one species
    "_unstated": "#A8C4D6",    # none named
}

# Dark-field colour -> imagery colour, so a palette entry can be translated
# without knowing which species produced it.
SPECIES_ORDER = ["bovine", "porcine", "poultry", "ovine", "caprine",
                 "equine", "cervid", "lagomorph", "fish", "crustacean",
                 "camelid", "canine", "mustelid", "reptile", "insect",
                 "farmed_game", "wild_game", "other"]


def _dot_key(species: list[str]) -> str:
    """Which palette entry a facility's dot uses.

    The species key, not the colour. Keying on the colour collapsed every
    species that shares a shade in the dark palette into one entry, so the
    imagery palette could only ever show as many hues as the dark one had --
    six species arrived on the map as the same green.
    """
    if not species:
        return "_unstated"
    primary = [s for s in SPECIES_ORDER if s in species]
    if not primary:
        return "_unstated"
    if len(primary) > 2:
        return "_mixed"
    return primary[0] if primary[0] in SPECIES_COLOUR else "other"


# Climate TRACE's confined-animal-facility asset definition, if a filtered
# extract has been dropped in. Kept as an overlay rather than parsed into
# facilities, and the distinction is not cosmetic: every other point on this map
# comes from a register that licensed a named premises, and these are a model's
# estimate that a facility exists at a location. Merging them would put modelled
# points into the dedup matcher and into the facility count, where a reader
# could not tell the two apart.
CAFO_PATHS = [Path("raw/climate_trace_cafo.geojsonl.gz"),
              Path("raw/climate_trace_cafo.geojsonl"),
              Path("raw/climate_trace_cafo_geojsonl.gz")]

# A painted world chart, reprojected to Web Mercator so it registers against
# the map's own CRS. It is the basemap at world view and dissolves into the
# satellite imagery underneath it as you zoom in, so the map reads as a drawn
# atlas until the point where imagery starts telling you something a drawing
# cannot -- the sheds, the lagoons, the stockyards.
PLATE_PATH = Path("atlas_plate.webp")
PLATE_SOUTH, PLATE_NORTH = -80.55, 85.05112877980659
PLATE_FADE_IN, PLATE_FADE_OUT = 3, 5      # zoom: full plate -> full imagery


def load_cafo(path: Path | None = None) -> dict | None:
    """Point overlay from a GeoJSONL (or GeoJSON) extract.

    Returns None when the file is absent, which is the normal case -- the layer
    then does not appear at all rather than appearing empty.
    """
    if path:
        f = Path(path)
    else:
        f = next((c for c in CAFO_PATHS if c.exists()), None)
    if f is None or not f.exists():
        return None

    feats = []
    # The extract arrives gzipped and is about six times smaller that way, so
    # it is read as-is rather than asking anyone to unpack it first.
    if f.suffix == ".gz":
        import gzip
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = f.read_text(encoding="utf-8")
    if f.suffix == ".geojson" or text.lstrip().startswith('{"type": "FeatureCollection"'):
        try:
            feats = json.loads(text).get("features", [])
        except json.JSONDecodeError:
            feats = []
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                feats.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # One row per facility per month. The archive runs 2021-01 to 2026-06, so an
    # extract that keeps every month carries the same site 66 times and would
    # draw it 66 times in the same pixel -- invisible on the map and wrong in
    # the count. Folded on the coordinate and the name, keeping the latest
    # month, so a single-month extract behaves identically and an all-months one
    # behaves sensibly.
    best: dict = {}
    periods = set()
    for ft in feats:
        geom = ft.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        try:
            x, y = geom["coordinates"][:2]
            x, y = float(x), float(y)
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        pr = ft.get("properties") or {}
        if pr.get("x_asset_definition") not in (None, "confined-animal-facility"):
            continue          # an unfiltered extract would otherwise slip through
        # The name field holds a generated identifier -- "ESP_MatureDairyCattle_1"
        # -- not a name anyone would recognise, and at this row count the strings
        # outweigh the coordinates in the published file. The part before the
        # last underscore is the only informative piece, and it repeats, so it
        # is kept once in a lookup rather than 285,000 times.
        raw_name = (pr.get("name") or "").strip()
        nm = raw_name.rsplit("_", 1)[0] if "_" in raw_name else raw_name
        period = str(pr.get("x_period") or "")
        if period:
            periods.add(period)
        key = (round(x, 5), round(y, 5), nm)
        if key in best and period <= best[key][0]:
            continue
        cap = pr.get("x_capacity")
        units = (pr.get("x_capacity_units") or "").strip()
        best[key] = (period, {
            "lon": round(x, 5), "lat": round(y, 5), "name": nm,
            # x_precision records whether a point is a located facility or an
            # administrative centroid. Anything not explicitly precise is drawn
            # hollow: a solid dot asserts a position the source did not give.
            # "asset" is Climate TRACE's own word for a located facility; the
            # rest of their vocabulary describes areas and grid cells.
            "precise": 1 if str(pr.get("x_precision", "")).lower()
                       in ("asset", "facility", "high", "exact", "point") else 0,
            "owner": (pr.get("x_owner") or "").strip(),
            "capacity": f"{cap} {units}".strip() if cap not in (None, "") else "",
        })

    rows = [v for _, v in best.values()]
    if not rows:
        return None
    # Dictionary-encode the repeated strings: 285,000 rows share a few hundred
    # distinct kind-labels and capacity values between them.
    kinds: dict = {}
    def idx(v):
        if v not in kinds:
            kinds[v] = len(kinds)
        return kinds[v]
    # Four decimal places is about eleven metres, which is finer than a modelled
    # facility position can honestly claim and a fifth off the file size.
    return {"lat": [round(r["lat"], 4) for r in rows],
            "lon": [round(r["lon"], 4) for r in rows],
            "kind": [idx(r["name"]) for r in rows],
            "kinds": [k for k, _ in sorted(kinds.items(), key=lambda kv: kv[1])],
            "precise": [r["precise"] for r in rows],
            "periods": sorted(periods), "n": len(rows),
            "rows_read": len(feats)}


def load_cultivated_laws() -> dict | None:
    """Laws against meat grown from cells, and the shapes to draw them on.

    A different kind of layer from the rest: not a place where something
    happens, but a line drawn around where something is not allowed to. It is
    on the map because the argument about slaughter is now partly an argument
    about what may replace it, and that argument is being settled state by
    state rather than anywhere central.
    """
    laws = Path("cultivated_meat_laws.json")
    if not laws.exists():
        return None
    data = json.loads(laws.read_text(encoding="utf-8"))
    states = Path("us_states.json")
    data["us_shapes"] = (json.loads(states.read_text(encoding="utf-8"))
                         if states.exists() else [])
    return data


def load_outlines() -> list:
    """Simplified country outlines, embedded rather than fetched.

    This replaces the tile layer outright. Hosted basemaps keep moving behind
    API keys -- CARTO now stamps "API key required" across the tiles -- and a
    map that depends on somebody else's key is a map that breaks without
    warning. 150 KB of Natural Earth outlines is the whole background, and the
    file has no network dependency left except Leaflet itself.

    Each entry is {"iso3": ..., "r": [ring, ...]} so the same geometry can do
    double duty: the background, and the coarse layer that shades a country by
    how many of its facilities have no address precise enough to place.
    """
    f = Path(__file__).parent / "world_outlines.json"
    return json.loads(f.read_text()) if f.exists() else []


# Activities this map does not draw on their own. Two kinds sit here: places
# that only ever handle an animal already dead, and places where animals are
# kept for something other than the food chain. A site listed under these and
# nothing else is held off the map; a site that also kills stays, which is why
# a plant listed `SH CP` -- slaughterhouse and cutting line -- is still drawn.
#
# This is a scope list, not a data filter. out/facilities.json.gz carries every
# row either way, the dedup report still counts them, and --keep-excluded draws
# them. Edit the set to change what the map shows.
EXCLUDED_ACTIVITIES = {
    # handled after killing
    "cutting", "processing", "minced_meat", "meat_preparations",
    "game_handling", "cold_store", "rendering", "casings", "egg_products",
    # kept for something other than meat, milk or eggs
    "experimentation", "zoo", "wildlife", "racing", "rodeo", "entertainment",
    "pet_breeder", "pet_shop", "agricultural_show",
    "farm_honey", "farm_wool", "farm_skins", "insect_rearing", "aquaculture",
}

# The old name, for anything still importing it.
POST_MORTEM_ONLY = EXCLUDED_ACTIVITIES


def drop_excluded(facilities):
    """(kept, dropped). A facility is dropped only when every activity it
    carries is on the excluded list AND no registry flagged it as slaughtering.
    One qualifying activity anywhere on the record keeps the whole site."""
    kept, dropped = [], []
    for f in facilities:
        acts = set(f.activities or [])
        if acts and acts <= EXCLUDED_ACTIVITIES and f.slaughter is not True:
            dropped.append(f)
        else:
            kept.append(f)
    return kept, dropped


drop_post_mortem_only = drop_excluded


def shares(facilities) -> dict:
    """Percentage of the whole dataset behind each filter row.

    Counted over every facility, located or not, because "12% of all plants
    slaughter" is a fact about the registries and should not change depending
    on how much of the geocoding has finished. A facility can carry several
    species and several activities, so these do not sum to 100 and the panel
    says so.
    """
    n = len(facilities) or 1
    c: Counter = Counter()
    for f in facilities:
        for a in (f.activities or ["_none"]):
            # An excluded activity is not a layer on this map. A site that also
            # kills is still drawn -- it is drawn as a slaughterhouse, and the
            # cutting line it also holds is not a filter of its own.
            if a in EXCLUDED_ACTIVITIES:
                continue
            c["What happens here::" + a] += 1
        for sp in (f.species or ["_none"]):
            c["Species::" + sp] += 1
        c["Slaughter activity::" + str({True: 1, False: 0, None: 2}[f.slaughter])] += 1
        for src in {m["source"] for m in f.members}:
            c["Registry::" + src] += 1
    return {k: round(v * 100 / n, 1) for k, v in c.items()}


def encode(facilities, sources_meta: dict) -> dict:
    """Compact payload. Only mappable facilities get geometry; the rest are
    carried in `unlocated` so they stay visible as a count and a list."""
    mappable = [f for f in facilities if f.mappable]
    unlocated = [f for f in facilities if not f.mappable]

    countries, species_combos, source_combos, tier_combos, palette = {}, {}, {}, {}, {}

    def idx(d: dict, key):
        if key not in d:
            d[key] = len(d)
        return d[key]

    (lat, lon, name, c_i, sp_i, act_i, src_i, tier_i, sl, uid, ids,
     ci) = ([] for _ in range(12))
    activity_combos: dict = {}

    for f in mappable:
        lat.append(round(f.lat * 1e5))
        lon.append(round(f.lon * 1e5))
        name.append(f.name)
        c_i.append(idx(countries, f.country_iso3 or "—"))
        sp_i.append(idx(species_combos, tuple(f.species)))
        act_i.append(idx(activity_combos, tuple(sorted(f.activities))))
        src_i.append(idx(source_combos, tuple(sorted({m["source"] for m in f.members}))))
        tier_i.append(idx(tier_combos, tuple(f.match_tiers)))
        sl.append({True: 1, False: 0, None: 2}[f.slaughter])
        uid.append(f.uid)
        ci.append(idx(palette, _dot_key(f.species)))
        ids.append([f"{m['source']}|{m.get('id_scheme') or ''}|{m.get('national_id') or ''}"
                    for m in f.members])

    def inv(d):
        return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1])]

    return {
        "shares": shares(facilities),
        "excluded_activities": sorted(EXCLUDED_ACTIVITIES),
        "n_all": len(facilities),
        "generated": date.today().isoformat(),
        "n_mapped": len(mappable),
        "n_unlocated": len(unlocated),
        "dict": {
            "country": inv(countries),
            "species": [list(t) for t in inv(species_combos)],
            "activity": [list(t) for t in inv(activity_combos)],
            "source": [list(t) for t in inv(source_combos)],
            "tier": [list(t) for t in inv(tier_combos)],
        },
        "palette": [SPECIES_COLOUR[k] for k in inv(palette)],
        # Same order, so one index looks up either.
        "palette_satellite": [SPECIES_COLOUR_SATELLITE[k] for k in inv(palette)],
        "colour": {s: SPECIES_COLOUR[s] for s in SPECIES_ORDER}
                  | {"_mixed": SPECIES_COLOUR["_mixed"],
                     "_unstated": SPECIES_COLOUR["_unstated"]},
        "colour_satellite": dict(SPECIES_COLOUR_SATELLITE),
        "lat": lat, "lon": lon, "name": name,
        "c": c_i, "sp": sp_i, "act": act_i, "src": src_i, "tier": tier_i, "sl": sl,
        "ci": ci, "uid": uid, "ids": ids,
        "unlocated": [{"name": f.name, "country": f.country_iso3,
                       "locality": f.locality, "uid": f.uid,
                       "sources": sorted({m["source"] for m in f.members})}
                      for f in unlocated],
        "sources_meta": sources_meta,
        "unlocated_by_country": dict(
            Counter(f.country_iso3 or "—" for f in unlocated).most_common()),
        "cafo": load_cafo(),
        "laws": load_cultivated_laws(),
        "outlines": load_outlines(),
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{
  --ink:#171d1b; --panel:#1e2523; --rail:#232c29;
  --text:#d6d3c8; --dim:#8a938c; --rule:rgba(214,211,200,.13);
  --live:#9fb09a;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--ink);color:var(--text);font-family:var(--sans)}
#map{position:absolute;inset:0;background:var(--ink)}
.leaflet-container{background:var(--ink)}

#rail{position:absolute;top:0;left:0;bottom:0;width:302px;z-index:600;
  background:var(--rail);border-right:1px solid var(--rule);
  display:flex;flex-direction:column;overflow:hidden}
#rail h1{font-family:var(--serif);font-weight:400;font-size:21px;line-height:1.2;
  margin:0;padding:20px 20px 4px}
#rail .sub{padding:0 20px 16px;color:var(--dim);font-size:12.5px;line-height:1.5}
.count{padding:0 20px 18px;border-bottom:1px solid var(--rule)}
.count b{font-family:var(--serif);font-size:34px;font-weight:400;display:block;
  line-height:1;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.count span{color:var(--dim);font-size:12px}
#filters{flex:1;overflow-y:auto;padding:6px 0 20px}
.grp{padding:14px 20px 6px}
.grp h2{font-size:11.5px;font-weight:600;color:var(--dim);margin:0 0 9px;
  letter-spacing:.02em}
label.row{display:flex;align-items:flex-start;gap:9px;padding:5px 0;cursor:pointer;
  font-size:13px;line-height:1.3}
label.row input{accent-color:var(--live);margin:3px 0 0;flex:none}
.swatch{width:9px;height:9px;border-radius:50%;flex:none;margin-top:4px}
.rowbody{flex:1;min-width:0}
.rowtop{display:flex;align-items:baseline;gap:8px}
.rowtop .lbl{flex:1}
.hint{display:block;color:var(--dim);font-size:11px;line-height:1.45;margin-top:1px}
.hint .share{color:var(--text);font-variant-numeric:tabular-nums}
.fate{display:inline-block;margin-top:3px;font-size:10.5px;line-height:1.5;
  padding:0 6px;border:1px solid currentColor;border-radius:9px;opacity:.85}
.kinds h4{font-size:12px;color:var(--text);margin:16px 0 2px;font-weight:600}
.kinds p{color:var(--dim);font-size:12px;line-height:1.6;margin:0 0 4px}
.kinds ul{margin:2px 0 0;padding-left:17px;color:var(--dim);font-size:12px;
  line-height:1.6}
.kinds li b{color:var(--text);font-weight:500}
.tally{margin-left:auto;color:var(--dim);font-size:11px;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.grp .lede{color:var(--dim);font-size:11px;line-height:1.5;margin:-4px 0 8px}
label.row.allrow{border-bottom:1px solid var(--rule);padding-bottom:8px;
  margin-bottom:4px}
label.row.allrow .lbl{color:var(--text);font-weight:600}
/* Leaflet's attribution strip is pinned to the bottom of the map and sits over
   whatever the rail ends with, which was cutting this note off mid-sentence. */
.note{padding:12px 20px 34px;color:var(--dim);font-size:11.5px;line-height:1.55;
  border-top:1px solid var(--rule)}
button.link{background:none;border:0;color:var(--live);font:inherit;font-size:12px;
  padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:2px}

#drawer{position:absolute;top:0;right:0;bottom:0;width:370px;z-index:610;
  background:var(--panel);border-left:1px solid var(--rule);
  transform:translateX(100%);transition:transform .22s ease;overflow-y:auto}
#drawer.open{transform:none}
@media (prefers-reduced-motion:reduce){#drawer{transition:none}}
/* Big enough to hit, and pushed down clear of the zoom buttons, which sit at
   the top right of the map and would otherwise land on top of it. */
#drawer .close{position:absolute;top:62px;right:12px;z-index:2;width:30px;
  height:30px;border-radius:50%;background:var(--ink);
  border:1px solid var(--rule);color:var(--text);font-size:17px;line-height:1;
  cursor:pointer;padding:0;display:flex;align-items:center;justify-content:center}
#drawer .close:hover{border-color:var(--live)}
#drawer h3{font-family:var(--serif);font-weight:400;font-size:19px;line-height:1.25;
  margin:0;padding:22px 56px 6px 20px}
#drawer .where{padding:0 20px 16px;color:var(--dim);font-size:12.5px;line-height:1.5}
.facts{padding:0 20px 18px;border-bottom:1px solid var(--rule)}
.fact{display:flex;gap:12px;padding:5px 0;font-size:13px}
.fact i{font-style:normal;color:var(--dim);flex:none;width:96px}
.prov{padding:16px 20px 26px}
.prov h4{font-size:11.5px;color:var(--dim);margin:0 0 3px;font-weight:600}
.prov p.lede{color:var(--dim);font-size:11.5px;line-height:1.5;margin:0 0 14px}
.rec{border-left:2px solid var(--rule);padding:2px 0 2px 13px;margin-bottom:15px}
.rec .who{font-size:12.5px;line-height:1.4}
.rec .num{font-size:12px;color:var(--live);font-variant-numeric:tabular-nums;
  margin-top:2px}
.rec .addr{font-size:12px;color:var(--dim);line-height:1.5;margin-top:3px}
.tiers{font-size:11.5px;color:var(--dim);line-height:1.55;padding-top:4px}
.pick{display:block;width:100%;text-align:left;background:none;border:0;
  border-left:2px solid var(--rule);color:var(--text);font:inherit;font-size:13px;
  padding:7px 0 7px 13px;cursor:pointer}
.pick:hover{border-left-color:var(--live)}

/* Esri's imagery is lit for a white page. Pulled back so the dots sit on it
   rather than fighting it, and only while an imagery basemap is on. */
#map.atlas .leaflet-tile-pane{filter:brightness(.70) saturate(.74) contrast(1.08)}
/* No mask on the plate. The southern edge used to be hidden behind a straight
   CSS gradient, which read as a ruled line across the ocean; the dissolve now
   lives in the image itself, ragged like the northern edge. A second fade on
   top of it would only cut the cartouche off. */
#dock{position:absolute;left:0;right:0;bottom:0;height:50%;z-index:620;
  background:var(--ink);border-top:1px solid var(--rule);
  display:flex;flex-direction:column;
  transform:translateY(100%);transition:transform .22s ease}
#dock.open{transform:none}
@media (prefers-reduced-motion:reduce){#dock{transition:none}}
.dock-bar{display:flex;align-items:center;gap:14px;padding:8px 12px 8px 16px;
  border-bottom:1px solid var(--rule);flex:none}
.dock-title{font-size:12.5px;color:var(--text);flex:1;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dock-out{font-size:11.5px;color:var(--live);text-decoration:none;flex:none}
.dock-out:hover{text-decoration:underline}
.dock-close{flex:none;width:28px;height:28px;border-radius:50%;
  background:none;border:1px solid var(--rule);color:var(--text);
  font-size:16px;line-height:1;cursor:pointer;padding:0}
.dock-close:hover{border-color:var(--live)}
.dock-body{position:relative;flex:1;min-height:0}
#dockFrame{position:absolute;inset:0;width:100%;height:100%;border:0}
.dock-note{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;text-align:center;padding:24px;margin:0;
  color:var(--dim);font-size:12.5px;line-height:1.6}
.dock-note b{color:var(--text);font-weight:600}
.dock-open{margin-top:2px;background:var(--live);border:0;border-radius:6px;
  color:var(--ink);font:inherit;font-size:12.5px;font-weight:600;
  padding:8px 14px;cursor:pointer}
.dock-open:hover{filter:brightness(1.1)}
@media (max-width:760px){#dock{height:60%}}
#boot{position:absolute;inset:0;z-index:1200;display:flex;align-items:center;
  justify-content:center;text-align:center;padding:40px;background:var(--ink);
  color:var(--dim);font-size:13px;line-height:1.6}
#boot.failed{color:var(--text)}
#boot .dim{color:var(--dim);font-size:11.5px}
#hover{position:absolute;z-index:620;pointer-events:none;background:var(--panel);
  border:1px solid var(--rule);padding:6px 9px;font-size:12px;max-width:250px;
  display:none;line-height:1.35}
#toggleRail{position:absolute;top:10px;left:10px;z-index:700;display:none;
  background:var(--rail);color:var(--text);border:1px solid var(--rule);
  padding:8px 12px;font:inherit;font-size:13px;cursor:pointer}
.leaflet-control-attribution{background:rgba(23,29,27,.86)!important;
  color:var(--dim)!important;font-size:10.5px!important}
.leaflet-control-attribution a{color:var(--dim)!important}
.leaflet-control-zoom a{background:var(--rail)!important;color:var(--text)!important;
  border-color:var(--rule)!important}
:focus-visible{outline:2px solid var(--live);outline-offset:2px}
@media (max-width:820px){
  #rail{transform:translateX(-100%);transition:transform .2s ease;width:280px}
  #rail.open{transform:none}
  #toggleRail{display:block}
  #drawer{width:100%}
}
</style>
</head>
<body>
<div id="map"></div>
<button id="toggleRail" aria-controls="rail">Filters</button>

<aside id="rail">
  <h1>__TITLE__</h1>
  __SUBTITLE_BLOCK__
  <div class="count"><b id="shown">0</b><span id="shownNote">facilities in view</span></div>
  <div id="filters"></div>
  <div class="note" id="unlocatedNote"></div>
</aside>

<aside id="drawer" aria-live="polite">
  <button class="close" id="closeDrawer" aria-label="Close details">&times;</button>
  <div id="drawerBody"></div>
</aside>

<div id="hover"></div>
<div id="boot">Loading&#8230;</div>

<!-- Counterglow opens in the bottom half of the window rather than replacing
     the page, so this map and theirs can be read against each other. Theirs is
     a register of reported farm locations; this one is a register of licences.
     Where they disagree is the interesting part. -->
<section id="dock" aria-hidden="true">
  <div class="dock-bar">
    <span class="dock-title">Counterglow &mdash; reported animal farm locations</span>
    <a class="dock-out" href="https://map.counterglow.org/" target="_blank"
       rel="noopener">Open in a new tab &#8599;</a>
    <button class="dock-close" id="dockClose" aria-label="Close">&times;</button>
  </div>
  <div class="dock-body">
    <p class="dock-note" id="dockNote">Loading Counterglow&#8230;</p>
    <iframe id="dockFrame" title="Counterglow map" loading="lazy"
            referrerpolicy="no-referrer"></iframe>
  </div>
</section>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script id="atlas-data" type="application/json"__DATA_SRC__>__DATA__</script>
<script>
(async function(){

/* The data can travel inside this file or beside it. Inline keeps the atlas a
   single self-contained document that works from a USB stick; split keeps the
   first paint fast, because the browser gets a 40 KB page instead of thirteen
   megabytes of JSON it has to parse before drawing anything. Split needs the
   page served over http -- fetch cannot read a sibling file from disk -- so
   the inline build stays available for offline use and for embedding. */
async function loadAtlasData(){
  const el = document.getElementById('atlas-data');
  const src = el.getAttribute('data-src');
  if(!src) return JSON.parse(el.textContent);

  /* Prefer the gzipped copy and unpack it here, so the transfer is a couple of
     megabytes whether or not the host compresses on the fly. GitHub Pages
     does; plenty of embed hosts do not. */
  if(typeof DecompressionStream === 'function'){
    try{
      const r = await fetch(src + '.gz');
      if(r.ok && r.body){
        const stream = r.body.pipeThrough(new DecompressionStream('gzip'));
        return JSON.parse(await new Response(stream).text());
      }
    }catch(e){ /* fall through to the plain file */ }
  }
  const r = await fetch(src);
  if(!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}

let D;
try{
  D = await loadAtlasData();
}catch(err){
  const b = document.getElementById('boot');
  b.innerHTML = '<span><b>The data file did not load.</b><br>It has to sit '+
    'next to this page, and the page has to be served over http rather than '+
    'opened from disk.<br><span class="dim">' +
    String((err && err.message) || err) + '</span></span>';
  b.className = 'failed';
  throw err;
}
document.getElementById('boot').remove();

const N = D.lat.length, TAU = Math.PI*2;

/* Every key in schema.SPECIES needs an entry. A missing one renders as the raw
   key and sorts to the top of the list, which is how "fish", "camelid",
   "canine" and "reptile" ended up above cattle in the rail. */
const SPECIES_LABEL = {bovine:'Cattle',porcine:'Pigs',poultry:'Poultry',ovine:'Sheep',
  caprine:'Goats',equine:'Horses',cervid:'Deer and elk',lagomorph:'Rabbits',
  fish:'Fish',crustacean:'Crustaceans and shellfish',camelid:'Camels and llamas',
  canine:'Dogs',mustelid:'Mink and ferrets',reptile:'Reptiles',insect:'Insects',
  farmed_game:'Farmed game',wild_game:'Wild game',other:'Other'};
const SLAUGHTER_LABEL = {1:'Slaughter confirmed by a registry',
  0:'Registry lists no slaughter activity', 2:'No registry stated either way'};

/* What the registries mean by each activity code, in plain words. The atlas is
   called an abattoir atlas, but only some of what the sources list is a kill
   floor -- the rest is where animals are held, raised, traded, cut up or kept
   cold. Saying which is which on the toggle is the difference between a map of
   slaughter and a map of the industry around it. */
const ACTIVITY_LABEL = {
  slaughter:'Slaughterhouse', farm_meat:'Meat farm', farm_dairy:'Dairy farm',
  farm_eggs:'Egg farm', farm_wool:'Wool farm', farm_skins:'Skin and fur farm',
  farm_poultry:'Poultry establishment', transporter:'Authorised transporter',
  insect_rearing:'Isolated bee rearing', germinal_products:'Germinal products',
  quarantine:'Quarantine establishment',
  farm_honey:'Apiary', hatchery:'Hatchery', saleyard:'Saleyard',
  live_market:'Live animal market', holding_yard:'Holding yard',
  aquaculture:'Aquaculture', experimentation:'Laboratory', zoo:'Zoo',
  wildlife:'Wildlife facility', racing:'Racing', rodeo:'Rodeo',
  entertainment:'Entertainment', pet_breeder:'Pet breeder', pet_shop:'Pet shop',
  agricultural_show:'Agricultural show', cutting:'Cutting plant',
  processing:'Processing plant', minced_meat:'Minced meat plant',
  meat_preparations:'Meat preparations', game_handling:'Game handling',
  cold_store:'Cold store', rendering:'Rendering plant', casings:'Casings plant',
  egg_products:'Egg products plant', unknown:'Activity not stated',
  _none:'Activity not stated'};
const ACTIVITY_NOTE = {
  slaughter:'Animals are killed here',
  farm_meat:'Animals confined and raised to be killed elsewhere',
  farm_dairy:'Cows confined for milk. A dairy cow is slaughtered when her '+
    'yield falls, usually at five or six years against a natural twenty; her '+
    'calves go to slaughter too',
  farm_eggs:'Hens confined for eggs. A laying hen is slaughtered when she '+
    'moults, and the male chicks were killed at the hatchery',
  farm_poultry:'Birds confined for breeding or production; the register does not say which',
  transporter:'A haulier licensed to move live animals on journeys over eight hours. The address is a company office, not a place animals are kept',
  farm_wool:'Sheep confined for wool', farm_skins:'Animals confined for skin or fur',
  farm_honey:'Bees kept for honey',
  insect_rearing:'Bumble bees reared in environmental isolation, for pollination rather than honey',
  germinal_products:'Semen, ova or embryos taken from breeding animals. Some are centres where donor animals are kept, some are teams that travel to them', hatchery:'Chicks hatched and sorted by sex. The males are killed within a '+
    'day of hatching because they will not lay',
  saleyard:'Animals bought and sold. Almost every animal that passes through '+
    'one is on its way to a place where it is killed',
  quarantine:'Animals held in compulsory isolation before they may move or enter a country',
  live_market:'Animals sold alive, often killed on site',
  holding_yard:'Animals held in transit. Almost every animal that passes '+
    'through one is on its way to a place where it is killed', aquaculture:'Fish or shellfish farmed',
  experimentation:'Animals used in research', zoo:'Animals held on display',
  wildlife:'Wild animals held or handled', racing:'Animals raced',
  rodeo:'Animals used in rodeo', entertainment:'Animals used in performance',
  pet_breeder:'Animals bred to sell', pet_shop:'Animals sold as pets',
  agricultural_show:'Animals exhibited',
  cutting:'Carcasses cut into pieces after killing',
  processing:'Meat processed; no killing stated',
  minced_meat:'Meat minced', meat_preparations:'Meat prepared for sale',
  game_handling:'Hunted animals brought in and dressed',
  cold_store:'Carcasses and meat held cold',
  rendering:'Bodies and offcuts rendered down',
  casings:'Intestines processed into casings', egg_products:'Eggs processed',
  unknown:'The registry listed this plant without saying what it does',
  _none:'The registry listed this plant without saying what it does'};
/* Whether the animals in a given kind of place are killed, and where. This is
   the question the map is most likely to be misread on: a dot is not a kill
   floor unless it says so, and a dairy is not a place where nothing dies. */
const FATE = {
  here:{tag:'killed here',           colour:'#a4635c'},
  transit:{tag:'animals not kept here', colour:'#6d8b93'},
  elsewhere:{tag:'killed elsewhere', colour:'#a1808d'},
  some:{tag:'some killed',           colour:'#8b8474'},
  after:{tag:'bodies handled here',  colour:'#6d8b93'},
  no:{tag:'not killed here',         colour:'#7e8e6c'}};
const ACTIVITY_FATE = {
  slaughter:'here', live_market:'here', aquaculture:'elsewhere',
  farm_meat:'elsewhere', farm_dairy:'elsewhere', farm_eggs:'elsewhere',
  farm_wool:'elsewhere', farm_skins:'elsewhere', hatchery:'some',
  farm_poultry:'elsewhere', transporter:'transit', insect_rearing:'no',
  quarantine:'elsewhere',
  germinal_products:'no',
  farm_honey:'no', saleyard:'elsewhere', holding_yard:'elsewhere',
  cutting:'after', processing:'after', minced_meat:'after',
  meat_preparations:'after', game_handling:'after', cold_store:'after',
  rendering:'after', casings:'after', egg_products:'after',
  experimentation:'some', zoo:'some', wildlife:'some', racing:'some',
  rodeo:'some', entertainment:'some', pet_breeder:'some', pet_shop:'some',
  agricultural_show:'no'};
const ACTIVITY_ORDER = ['slaughter','live_market','saleyard','holding_yard',
  'quarantine','transporter',
  'farm_meat','farm_dairy','farm_poultry','farm_eggs','farm_wool','farm_skins',
  'farm_honey',
  'hatchery','aquaculture','insect_rearing','germinal_products','cutting',
  'game_handling',
  'processing','minced_meat',
  'meat_preparations','casings','egg_products','rendering','cold_store',
  'experimentation','zoo','wildlife','racing','rodeo','entertainment',
  'pet_breeder','pet_shop','agricultural_show','unknown','_none'];
const SLAUGHTER_NOTE = {
  1:'A registry field says animals are killed on site',
  0:'The registry names what the place does and killing is not among it \u2014 '+
    'a cutting plant, a cold store, a haulier, a hatchery',
  2:'The registry is silent, which is not the same as a no'};

/* ---- project once ----------------------------------------------------
   Web Mercator, normalised to 0..1. Per frame this becomes two multiplies and
   a subtract per point, which is what makes drawing every single facility
   cheap enough to do at world zoom. */
const WX = new Float64Array(N), WY = new Float64Array(N);
for(let i=0;i<N;i++){
  const la = D.lat[i]/1e5, lo = D.lon[i]/1e5;
  WX[i] = (lo+180)/360;
  const s = Math.sin(la*Math.PI/180);
  WY[i] = 0.5 - Math.log((1+s)/(1-s))/(4*Math.PI);
}

/* ---- pre-group by colour so fillStyle is set a handful of times ------- */
const GROUPS = D.palette.map((col,i)=>({
  colour:col,
  colourSat:(D.palette_satellite||[])[i] || col,
  idx:[]}));
for(let i=0;i<N;i++) GROUPS[D.ci[i]].idx.push(i);
for(const g of GROUPS) g.idx = Int32Array.from(g.idx);

/* ---- filter state; VIS is rebuilt only when a filter changes ---------- */
const VIS = new Uint8Array(N);
const active = {species:new Set(), slaughter:new Set([0,1,2]), source:new Set(),
                activity:new Set()};
D.dict.species.forEach(c=>c.forEach(s=>active.species.add(s)));
active.species.add('_none');
(D.dict.activity||[]).forEach(c=>c.forEach(a=>active.activity.add(a)));
active.activity.add('_none');
D.dict.source.forEach(c=>c.forEach(s=>active.source.add(s)));

function rebuildVis(){
  for(let i=0;i<N;i++){
    let ok = active.slaughter.has(D.sl[i]);
    if(ok){
      const sp = D.dict.species[D.sp[i]].filter(s=>!EXCLUDED_SPECIES.has(s));
      ok = sp.length===0 ? active.species.has('_none')
                         : sp.some(s=>active.species.has(s));
    }
    if(ok){
      let ac = (D.dict.activity&&D.act) ? D.dict.activity[D.act[i]] : null;
      if(ac){
        ac = ac.filter(a=>!EXCLUDED_ACT.has(a));
        ok = ac.length===0 ? active.activity.has('_none')
                           : ac.some(a=>active.activity.has(a));
      }
    }
    if(ok) ok = D.dict.source[D.src[i]].some(s=>active.source.has(s));
    VIS[i] = ok?1:0;
  }
  updateTallies();
}

const map = L.map('map',{worldCopyJump:true,zoomControl:false,
  attributionControl:true}).setView([26,8],3);
/* Top right: the left is where the rail opens from on a narrow screen, and the
   zoom buttons sat under it. */
L.control.zoom({position:'topright'}).addTo(map);
map.attributionControl.addAttribution(
  'Facility records from national and EU/China export registries &middot; '+
  'outlines from <a href="https://www.naturalearthdata.com/">Natural Earth</a>');

/* Country outlines, drawn from embedded geometry. No tile server, so no key
   to expire and nothing to rate-limit. */
const OUT = (D.outlines||[]).map(country => {
  const raw = Array.isArray(country) ? country : country.r;      // v1 or v2
  return {
    iso3: Array.isArray(country) ? null : country.iso3,
    rings: raw.map(ring => {
      const wx = new Float64Array(ring.length), wy = new Float64Array(ring.length);
      for(let i=0;i<ring.length;i++){
        wx[i] = (ring[i][0]+180)/360;
        const s = Math.sin(ring[i][1]*Math.PI/180);
        wy[i] = 0.5 - Math.log((1+s)/(1-s))/(4*Math.PI);
      }
      return {wx, wy};
    })
  };
});

/* Path-building is shared by the background and the coarse layer below. */
function tracePath(ctx, rings, S, offX, offY, w, h){
  for(const ring of rings){
    const {wx,wy} = ring;
    let started=false, any=false;
    for(let i=0;i<wx.length;i++){
      const x=wx[i]*S-offX, y=wy[i]*S-offY;
      if(x>-2000 && x<w+2000 && y>-2000 && y<h+2000) any=true;
      if(!started){ ctx.moveTo(x,y); started=true; } else ctx.lineTo(x,y);
    }
    if(any) ctx.closePath();
  }
}

const Base = L.Layer.extend({
  onAdd(m){
    this._c = L.DomUtil.create('canvas','leaflet-zoom-animated');
    this._ctx = this._c.getContext('2d');
    m.getPanes().tilePane.appendChild(this._c);
    m.on('moveend zoomend resize',this.draw,this);
    if(m.options.zoomAnimation) m.on('zoomanim',this._anim,this);
    this.draw();
  },
  onRemove(m){
    m.off('moveend zoomend resize',this.draw,this);
    m.off('zoomanim',this._anim,this);
    L.DomUtil.remove(this._c);
  },
  _anim(e){
    const s=this._map.getZoomScale(e.zoom), o=this._map._latLngToNewLayerPoint(
      this._map.getBounds().getNorthWest(), e.zoom, e.center);
    L.DomUtil.setTransform(this._c,o,s);
  },
  draw(){
    const m=this._map; if(!m) return;
    const size=m.getSize(), dpr=window.devicePixelRatio||1;
    const tl=m.containerPointToLayerPoint([0,0]);
    L.DomUtil.setPosition(this._c,tl);
    this._c.width=size.x*dpr; this._c.height=size.y*dpr;
    this._c.style.width=size.x+'px'; this._c.style.height=size.y+'px';
    const ctx=this._ctx; ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,size.x,size.y);

    const z=m.getZoom(), S=256*Math.pow(2,z), o=m.getPixelOrigin();
    const offX=o.x+tl.x, offY=o.y+tl.y, w=size.x, h=size.y;

    ctx.fillStyle='#202825';               /* land */
    ctx.strokeStyle='rgba(214,211,200,.17)';
    ctx.lineWidth = z < 4 ? 0.6 : 0.9;
    ctx.beginPath();
    for(const country of OUT) tracePath(ctx, country.rings, S, offX, offY, w, h);
    ctx.fill('evenodd');
    ctx.stroke();
  }
});
const outlineBase = new Base();
outlineBase.addTo(map);

/* Esri World Imagery: free, no API key, no sign-up. The outlines above stay
   the default because they are embedded in this file and work with no network
   at all; imagery is a second pair of eyes for when you want to see the sheds,
   the lagoons and the yards rather than a dot on a plain field.

   maxNativeZoom stops at the deepest zoom Esri actually serves and lets the
   map keep zooming past it on stretched tiles, so the dots do not hit a wall
   at z19. */
let SATELLITE = false;
const imagery = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {noWrap:true, bounds:[[-85.05,-180],[85.05,180]],
   maxNativeZoom:19, maxZoom:22, updateWhenZooming:false, keepBuffer:2,
   attribution:'Imagery &copy; <a href="https://www.esri.com/">Esri</a>, Maxar, '+
     'Earthstar Geographics, USDA FSA, USGS, Aerogrid, IGN, IGP and the GIS '+
     'user community'});

/* Coastlines, borders and place names over the imagery. Without them the world
   view is a photograph with nothing to read a location against. */
const places = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/'+
  'World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
  {noWrap:true, bounds:[[-85.05,-180],[85.05,180]],
   opacity:.6, maxNativeZoom:16, maxZoom:22, updateWhenZooming:false,
   keepBuffer:2});

/* The painted plate: a drawn world chart reprojected to Web Mercator so it
   registers against this map's CRS, in its own pane over the tiles and under
   the vectors, taking no clicks. It dissolves into the imagery beneath it as
   you zoom, so the map is a drawing until imagery starts saying something a
   drawing cannot. */
const PLATE = D.plate || null;
let plateImg = null;
if(PLATE){
  map.createPane('plate');
  const pp = map.getPane('plate');
  pp.style.zIndex = 250;                 /* tiles 200, vectors 400 */
  pp.style.pointerEvents = 'none';
}

/* Data rasters need their own pane above the painted plate. In the tile pane
   they sit at 200, under the plate at 250, so at world view -- exactly where a
   density grid is worth looking at -- the painting hid them, and they only
   appeared once the plate had faded out around zoom 5. */
map.createPane('datatiles');
map.getPane('datatiles').style.zIndex = 260;
map.getPane('datatiles').style.pointerEvents = 'none';

function plateBlend(){
  if(!plateImg) return;
  const z = map.getZoom();
  const t = Math.max(0, Math.min(1, (z-PLATE.fin)/(PLATE.fout-PLATE.fin)));
  plateImg.setOpacity(1-t);
}

/* ---- FAO Gridded Livestock of the World -------------------------------- */
/* A modelled raster, and the only layer on this map that is not a place.
   Subnational census totals downscaled to a grid by a Random Forest model, so a
   pixel is an estimate of animals in that cell rather than animals counted
   there. No owner, no company, no farm behind any pixel. It sits under the
   points, because the points are evidence and this is context.

   FAO's catalogue prints TileCol={y} and TileRow={x}, the reverse of the OGC
   order, and shipping their version drew nothing at all. The conventional
   order is the default now; the catalogue's own stays one click away, because
   FAO are the publisher and may be describing a real quirk of their service.

   tilematrixset=EPSG:3857 is right rather than a guess: the same FAO endpoint
   returns an empty layer under EPSG:4326 and starts working once the matrix set
   matches the map's own projection. What the mapset serves when no species is
   named is still unverified. */
const GLW_ATTRIB =
  'Livestock density: FAO, <a href="https://data.apps.fao.org/catalog/iso/'+
  '9d1e149b-d63f-4213-978b-317a8eb42d02">Gridded Livestock of the World 4</a> '+
  '(2020), CC BY 4.0 &mdash; modelled, not counted';
const GLW_BASE = 'https://data.apps.fao.org/map/wmts/wmts'+
  '?layer=fao-gismgr/GLW4-2020/mapsets/D-DA'+
  '&tilematrixset=EPSG:3857&Service=WMTS&request=GetTile&Version=1.0.0'+
  /* style is mandatory and FAO's own published template leaves it out. The
     service answers a request without it with
     {"detail":"Missing 'GetTile' parameters: {'style'}"} rather than an image,
     which is why the layer drew nothing whichever axis order it was given. */
  '&style=default'+
  '&Format=image/png&layertype=Image';

/* style=default got tiles out of the service. Where they land is a second
   question, and the first answer put the grid in the sea off every coast --
   which is what a row axis counted from the wrong end looks like. WMTS numbers
   rows from the top, TMS from the bottom, and a server can advertise one while
   serving the other. Leaflet writes the bottom-up form as {-y}.

   So the alternatives are back, narrowed to the four ways the two axes can be
   read. Pick the one that puts cattle on land. */
/* Settled by testing: standard WMTS, rows counted from the top. FAO's
   catalogue prints the column and row the other way round and omits style
   entirely; both were wrong, and the alternatives are gone now that the right
   one is known. */
const GLW_TILES = '&TileMatrix={z}&TileCol={x}&TileRow={y}';

let glwLayer = null, glwTried = 0, glwOk = 0;

function setLivestock(on){
  if(glwLayer){ map.removeLayer(glwLayer); glwLayer = null; }
  const status = document.getElementById('glwStatus');
  if(!on){ if(status) status.textContent=''; return; }

  glwTried = glwOk = 0;
  glwLayer = L.tileLayer(GLW_BASE + GLW_TILES, {
    pane: 'datatiles',
    /* One world, not three. Without this the grid repeats east and west of the
       map, the same as the basemap tiles would -- the imagery layers are
       already held to a single copy this way. */
    noWrap: true, bounds: [[-85.05,-180],[85.05,180]],
    opacity: 0.65, maxNativeZoom: 10, maxZoom: 22,
    attribution: GLW_ATTRIB,
    /* Leaflet loads tiles as <img>, so no CORS header is needed to draw them.
       It would be needed only to read their pixels back out of a canvas. */
  });
  /* A WMTS that rejects the request answers with an error image or a 404, and
     Leaflet reports that per tile. Counting them is the difference between
     "this variant is wrong" and "there is no livestock here". */
  glwLayer.on('tileload', function(){ glwTried++; glwOk++; report(); });
  glwLayer.on('tileerror', function(){ glwTried++; report(); });
  /* Silent while it works. It speaks up only if FAO change the address under
     us, which is the failure this layer is most exposed to. */
  function report(){
    if(!status) return;
    status.textContent = (glwOk || glwTried < 4) ? ''
      : 'No tiles are loading. FAO may have changed the layer address.';
  }
  glwLayer.addTo(map);
  if(map.hasLayer(layer)) layer.bringToFront();
}

function setBasemap(kind){
  SATELLITE = (kind!=='outlines');       /* both imagery modes light the dots */
  const mapEl = document.getElementById('map');
  if(SATELLITE){
    if(map.hasLayer(outlineBase)) map.removeLayer(outlineBase);
    if(!map.hasLayer(imagery)) imagery.addTo(map);
    if(!map.hasLayer(places)) places.addTo(map);
    mapEl.classList.add('atlas');
    if(kind==='atlas' && PLATE && !plateImg){
      plateImg = L.imageOverlay(PLATE.src, [[PLATE.s,-180],[PLATE.n,180]],
        {pane:'plate', interactive:false, className:'plate-img'});
      /* The painting is a file beside this page, and a missing file is silent:
         the layer simply never draws and the map looks like plain imagery. So
         say which file is missing, by name, rather than leaving it a mystery. */
      plateImg.on('error', function(){
        const n=document.getElementById('plateNote');
        if(n) n.innerHTML='<b>The painting did not load.</b> '+esc(PLATE.src)+
          ' has to sit in the same folder as this page.';
      });
      plateImg.addTo(map);
      map.on('zoomend', plateBlend);
    }else if(kind!=='atlas' && plateImg){
      map.off('zoomend', plateBlend);
      map.removeLayer(plateImg); plateImg = null;
    }
    plateBlend();
  }else{
    if(map.hasLayer(imagery)) map.removeLayer(imagery);
    if(map.hasLayer(places)) map.removeLayer(places);
    if(plateImg){ map.off('zoomend', plateBlend);
      map.removeLayer(plateImg); plateImg = null; }
    mapEl.classList.remove('atlas');
    if(!map.hasLayer(outlineBase)) outlineBase.addTo(map);
  }
  layer.draw();
}

/* ---- coarse layer: the facilities that cannot be placed --------------- */
/* Admin-1 polygons would be the honest resolution here -- 97.8% of these
   records name a region -- but a global admin-1 boundary set runs to tens of
   megabytes and there is no reliable join from the TRACES region string
   ("Creuse,New Aquitaine,Metropolitan France") to a boundary name. Country
   shading is what the embedded geometry can support truthfully: it says how
   many are missing and where, and claims nothing finer. */
const UNLOC_BY_C = D.unlocated_by_country || {};
const UNLOC_MAX = Math.max(1, ...Object.values(UNLOC_BY_C));
let SHOW_UNLOC = false;

const Coarse = L.Layer.extend({
  onAdd(m){
    this._c = L.DomUtil.create('canvas','leaflet-zoom-animated');
    this._ctx = this._c.getContext('2d');
    m.getPanes().overlayPane.appendChild(this._c);
    m.on('moveend zoomend resize',this.draw,this);
    if(m.options.zoomAnimation) m.on('zoomanim',this._anim,this);
    this.draw();
  },
  _anim(e){
    const s=this._map.getZoomScale(e.zoom), o=this._map._latLngToNewLayerPoint(
      this._map.getBounds().getNorthWest(), e.zoom, e.center);
    L.DomUtil.setTransform(this._c,o,s);
  },
  draw(){
    const m=this._map; if(!m) return;
    const size=m.getSize(), dpr=window.devicePixelRatio||1;
    const tl=m.containerPointToLayerPoint([0,0]);
    L.DomUtil.setPosition(this._c,tl);
    this._c.width=size.x*dpr; this._c.height=size.y*dpr;
    this._c.style.width=size.x+'px'; this._c.style.height=size.y+'px';
    const ctx=this._ctx; ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,size.x,size.y);
    if(!SHOW_UNLOC) return;

    const z=m.getZoom(), S=256*Math.pow(2,z), o=m.getPixelOrigin();
    const offX=o.x+tl.x, offY=o.y+tl.y, w=size.x, h=size.y;
    for(const country of OUT){
      const n = country.iso3 ? UNLOC_BY_C[country.iso3] : 0;
      if(!n) continue;
      /* Square root rather than linear: one country holds several thousand and
         most hold a handful, and a linear ramp would render all but the top
         two invisible. */
      ctx.globalAlpha = 0.12 + 0.5*Math.sqrt(n/UNLOC_MAX);
      ctx.fillStyle = '#a4635c';
      ctx.beginPath();
      tracePath(ctx, country.rings, S, offX, offY, w, h);
      ctx.fill('evenodd');
    }
    ctx.globalAlpha=1;
  }
});
const coarse = new Coarse();

/* ---- one canvas, one point per facility ------------------------------ */
let geom = {S:0, offX:0, offY:0, w:0, h:0};

const Layer = L.Layer.extend({
  onAdd(m){
    this._c = L.DomUtil.create('canvas','leaflet-zoom-animated');
    this._ctx = this._c.getContext('2d',{alpha:true});
    m.getPanes().overlayPane.appendChild(this._c);
    m.on('moveend zoomend resize',this.draw,this);
    if(m.options.zoomAnimation) m.on('zoomanim',this._anim,this);
    this.draw();
  },
  _anim(e){
    const s=this._map.getZoomScale(e.zoom), o=this._map._latLngToNewLayerPoint(
      this._map.getBounds().getNorthWest(), e.zoom, e.center);
    L.DomUtil.setTransform(this._c,o,s);
  },
  draw(){
    /* A draw before the layer joins the map is a no-op, not a crash. This
       script is one async function, so any throw in it stops everything after
       the throw -- one early call left the whole map blank. */
    const m=this._map; if(!m) return;
    const size=m.getSize(), dpr=window.devicePixelRatio||1;
    const tl=m.containerPointToLayerPoint([0,0]);
    L.DomUtil.setPosition(this._c,tl);
    this._c.width=size.x*dpr; this._c.height=size.y*dpr;
    this._c.style.width=size.x+'px'; this._c.style.height=size.y+'px';
    const ctx=this._ctx;
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,size.x,size.y);

    const z=m.getZoom(), S=256*Math.pow(2,z);
    const o=m.getPixelOrigin();
    geom={S, offX:o.x+tl.x, offY:o.y+tl.y, w:size.x, h:size.y};
    const offX=geom.offX, offY=geom.offY, w=geom.w, h=geom.h;

    /* Dot size and opacity climb with zoom. At world view the dots are small
       and semi-transparent, so a dozen plants stacked in one valley read as a
       deeper patch of colour instead of one dot hiding eleven others. */
    let r, alpha, useRect, ring=false;
    if(z<4){ r=1.1; alpha=.50; useRect=true; }
    else if(z<6){ r=1.4; alpha=.58; useRect=true; }
    else if(z<8){ r=1.8; alpha=.68; useRect=true; }
    else if(z<10){ r=2.7; alpha=.82; useRect=false; }
    else { r=4.3; alpha=.92; useRect=false; ring=true; }
    /* Over imagery the background is bright and busy rather than flat and
       dark, so the same alpha reads as a smudge. The dots also switch to the
       lighter palette and get a dark outline at every zoom, not just close in:
       a light fill with a dark edge is the one combination that holds against
       both a pale ploughed field and dark woodland. */
    if(SATELLITE){ alpha=Math.min(1,alpha+.3); r+=.7; ring=true; }

    let count=0;
    const d=r*2, wrap = z<6 ? S : 0;   // second world copy near the dateline

    for(const g of GROUPS){
      const arr=g.idx, n=arr.length;
      ctx.fillStyle = SATELLITE ? (g.colourSat || g.colour) : g.colour;
      ctx.globalAlpha=alpha;
      if(useRect && !SATELLITE){
        for(let k=0;k<n;k++){
          const i=arr[k]; if(!VIS[i]) continue;
          let x=WX[i]*S-offX;
          if(wrap){ if(x<-d) x+=wrap; else if(x>w+d) x-=wrap; }
          if(x<-d||x>w+d) continue;
          const y=WY[i]*S-offY;
          if(y<-d||y>h+d) continue;
          ctx.fillRect(x-r,y-r,d,d);
          count++;
        }
      }else{
        ctx.beginPath();
        for(let k=0;k<n;k++){
          const i=arr[k]; if(!VIS[i]) continue;
          const x=WX[i]*S-offX; if(x<-d||x>w+d) continue;
          const y=WY[i]*S-offY; if(y<-d||y>h+d) continue;
          ctx.moveTo(x+r,y); ctx.arc(x,y,r,0,TAU);
          count++;
        }
        ctx.fill();
        if(ring){
          ctx.globalAlpha=SATELLITE?.9:.7;
          ctx.lineWidth=SATELLITE?1.2:1;
          ctx.strokeStyle=SATELLITE?'rgba(18,20,16,.85)':'rgba(23,29,27,.9)';
          ctx.stroke();
        }
      }
    }
    ctx.globalAlpha=1;

    document.getElementById('shown').textContent=count.toLocaleString();
    document.getElementById('shownNote').textContent=
      count===1?'facility in view':'facilities in view';
  }
});
const layer=new Layer();

/* ---- hit testing: linear scan over the same arrays, rAF-throttled ----- */
function near(px,py,radius){
  const S=geom.S, offX=geom.offX, offY=geom.offY, rr=radius*radius;
  const out=[];
  for(let i=0;i<N;i++){
    if(!VIS[i]) continue;
    const dx=WX[i]*S-offX-px; if(dx>radius||dx<-radius) continue;
    const dy=WY[i]*S-offY-py; if(dy>radius||dy<-radius) continue;
    const d2=dx*dx+dy*dy;
    if(d2<=rr) out.push([d2,i]);
  }
  out.sort((a,b)=>a[0]-b[0]);
  return out.map(p=>p[1]);
}

const hover=document.getElementById('hover');
let pending=null, rafId=0;
map.on('mousemove',e=>{
  pending=e.containerPoint;
  if(rafId) return;
  rafId=requestAnimationFrame(()=>{
    rafId=0;
    const p=pending, z=map.getZoom();
    const hits=near(p.x,p.y, z<8?5:8);
    if(!hits.length){hover.style.display='none';map.getContainer().style.cursor='';return;}
    map.getContainer().style.cursor='pointer';
    hover.style.display='block';
    hover.style.left=(p.x+14)+'px'; hover.style.top=(p.y+12)+'px';
    hover.textContent = hits.length===1 ? D.name[hits[0]]
      : D.name[hits[0]]+' and '+(hits.length-1)+' more here';
  });
});
map.on('mouseout',()=>hover.style.display='none');
map.on('click',e=>{
  const hits=near(e.containerPoint.x,e.containerPoint.y, map.getZoom()<8?5:8);
  if(!hits.length) return;
  if(hits.length===1) openDrawer(hits[0]); else openPicker(hits);
});

/* ---- drawer ---------------------------------------------------------- */
const drawer=document.getElementById('drawer'), body=document.getElementById('drawerBody');
document.getElementById('closeDrawer').onclick=()=>drawer.classList.remove('open');
const TIER_TEXT={
  id:'matching establishment numbers in the same registry scheme',
  addr:'same postcode and street number, with matching names',
  geo:'coordinates within 250 m, with matching names',
};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function openPicker(hits){
  /* Several distinct facilities under the cursor. They are separate plants, so
     they get listed as separate plants rather than summed into a count. */
  let h='<h3>'+hits.length+' facilities at this point</h3>';
  h+='<p class="where">Close enough together to overlap at this zoom. Zoom in '+
     'to separate them, or pick one.</p><div class="prov">';
  for(const i of hits.slice(0,40)){
    h+='<button class="pick" data-i="'+i+'">'+esc(D.name[i])+
       '<span style="color:var(--dim)"> &middot; '+esc(D.dict.country[D.c[i]])+
       '</span></button>';
  }
  h+='</div>';
  body.innerHTML=h; drawer.classList.add('open');
  body.querySelectorAll('.pick').forEach(b=>b.onclick=()=>openDrawer(+b.dataset.i));
}

function openDrawer(i){
  const sp=D.dict.species[D.sp[i]], srcs=D.dict.source[D.src[i]],
        tiers=D.dict.tier[D.tier[i]], meta=D.sources_meta||{};
  const recs=D.ids[i].map(s=>{const p=s.split('|');
    return {src:p[0],scheme:p[1],num:p[2]};});
  let h='<h3>'+esc(D.name[i])+'</h3>';
  h+='<p class="where">'+esc(D.dict.country[D.c[i]])+'</p>';
  h+='<div class="facts">';
  h+='<div class="fact"><i>Slaughter</i><span>'+SLAUGHTER_LABEL[D.sl[i]]+'</span></div>';
  h+='<div class="fact"><i>Species</i><span>'+
     (sp.length?sp.map(s=>SPECIES_LABEL[s]||s).join(', '):'Not stated')+'</span></div>';
  h+='<div class="fact"><i>Listed by</i><span>'+srcs.length+
     (srcs.length===1?' registry':' registries')+'</span></div></div>';
  h+='<div class="prov"><h4>Where this record comes from</h4>';
  h+='<p class="lede">Each entry below is one registry\'s own listing, kept as '+
     'that registry published it.</p>';
  for(const r of recs){
    const label=(meta[r.src]&&meta[r.src].name)||r.src;
    h+='<div class="rec"><div class="who">'+esc(label)+'</div>';
    if(r.num) h+='<div class="num">'+esc(r.scheme?r.scheme+' '+r.num:r.num)+'</div>';
    h+='</div>';
  }
  if(recs.length>1){
    h+='<div class="tiers">Joined into one facility by '+
       (tiers.length?tiers.map(t=>TIER_TEXT[t]||t).join('; '):'a manual decision')+'.</div>';
  }
  h+='</div>';
  body.innerHTML=h; drawer.classList.add('open');
}

/* ---- filter UI ------------------------------------------------------- */
const F=document.getElementById('filters');
const SHARES=D.shares||{};

/* Each toggle carries two things the label alone does not: the share of the
   whole dataset behind it, and a line saying what that kind of place actually
   is. A map of 72,000 dots is not a map of 72,000 kill floors, and the panel
   should not let anyone assume it is. */
function group(title,items,set,swatches,lede,allSwitch){
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>'+title+'</h2>';
  if(lede){ const p=document.createElement('p'); p.className='lede';
    p.textContent=lede; g.appendChild(p); }

  /* One switch for the whole group. With thirteen kinds of place in this list,
     clearing them one at a time to look at a single one is thirteen clicks. */
  let allBox=null;
  if(allSwitch){
    const al=document.createElement('label'); al.className='row allrow';
    allBox=document.createElement('input'); allBox.type='checkbox';
    allBox.checked=true;
    al.appendChild(allBox);
    const ab=document.createElement('div'); ab.className='rowbody';
    const at=document.createElement('div'); at.className='rowtop';
    const an=document.createElement('span'); an.className='lbl';
    an.textContent='All of these';
    at.appendChild(an); ab.appendChild(at); al.appendChild(ab);
    g.appendChild(al);
    allBox.onchange=function(){
      const on=allBox.checked;
      set.clear();
      for(const it of items) if(on) set.add(it.key);
      g.querySelectorAll('label.row:not(.allrow) input').forEach(cb=>{
        cb.checked=on;
      });
      rebuildVis(); layer.draw();
    };
  }
  for(const it of items){
    const l=document.createElement('label'); l.className='row';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=set.has(it.key);
    cb.onchange=function(){ if(cb.checked) set.add(it.key); else set.delete(it.key);
      if(allBox){
        const boxes=[...g.querySelectorAll('label.row:not(.allrow) input')];
        allBox.checked = boxes.every(x=>x.checked);
        allBox.indeterminate = !allBox.checked && boxes.some(x=>x.checked);
      }
      rebuildVis(); layer.draw(); };
    l.appendChild(cb);
    if(swatches&&it.colour){const s=document.createElement('span');
      s.className='swatch'; s.style.background=it.colour; l.appendChild(s);}

    const body=document.createElement('div'); body.className='rowbody';
    const top=document.createElement('div'); top.className='rowtop';
    const t=document.createElement('span'); t.className='lbl'; t.textContent=it.label;
    const n=document.createElement('span'); n.className='tally';
    n.dataset.k=title+'::'+it.key;
    top.appendChild(t); top.appendChild(n); body.appendChild(top);

    const pct=SHARES[title+'::'+it.key];
    const bits=[];
    /* Two numbers sat side by side with nothing saying which was which: a live
       count of what is currently drawn, and a share of the whole dataset. They
       can disagree wildly and correctly -- a registry can hold a quarter of all
       the facilities and have a few hundred drawn, because the rest have no
       address precise enough to place. Both are labelled now. */
    /* The count above is what is drawn right now; this is the share of every
       facility on file, including the ones with no usable address. The two
       differ by about 19,000, and "of all" left that gap unexplained. */
    if(pct!==undefined) bits.push('<span class="share">'+pct.toFixed(1)+'%</span> of the '+
      (D.n_all||0).toLocaleString()+' listed');
    if(it.note) bits.push(esc(it.note));
    if(bits.length){ const h=document.createElement('span'); h.className='hint';
      h.innerHTML=bits.join(' &middot; '); body.appendChild(h); }
    if(it.fate&&FATE[it.fate]){
      const f=document.createElement('span'); f.className='fate';
      f.textContent=FATE[it.fate].tag; f.style.color=FATE[it.fate].colour;
      body.appendChild(f);
    }

    l.appendChild(body);
    g.appendChild(l);
  }
  F.appendChild(g);
}

/* ---- basemap ---------------------------------------------------------- */
(function(){
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>Basemap</h2>';
  const note=document.createElement('p'); note.className='lede';
  note.id='plateNote';
  const opts=[];
  if(D.plate) opts.push({k:'atlas',label:'Painted atlas',
    note:'A drawn world chart at this scale, dissolving into satellite imagery '+
         'as you zoom in. By the time one plant fills the screen the drawing '+
         'is gone and you are looking at the sheds and the lagoons.'});
  opts.push(
    {k:'satellite',label:'Satellite imagery',
     note:'Esri World Imagery with coastlines and place names over it, and no '+
          'painted layer at any zoom.'},
    {k:'outlines',label:'Country outlines',
     note:'Embedded in this file. No tile server, nothing to expire, works offline.'});
  if(D.plate) g.appendChild(note);
  const firstKind = opts[0].k;
  for(const o of opts){
    const l=document.createElement('label'); l.className='row';
    const rb=document.createElement('input'); rb.type='radio'; rb.name='basemap';
    rb.checked=(o.k===firstKind);
    rb.onchange=function(){ if(rb.checked) setBasemap(o.k); };
    l.appendChild(rb);
    const body=document.createElement('div'); body.className='rowbody';
    const top=document.createElement('div'); top.className='rowtop';
    const t=document.createElement('span'); t.className='lbl'; t.textContent=o.label;
    top.appendChild(t); body.appendChild(top);
    const h=document.createElement('span'); h.className='hint'; h.textContent=o.note;
    body.appendChild(h); l.appendChild(body); g.appendChild(l);
  }
  F.appendChild(g);
})();

/* ---- what kind of place ----------------------------------------------- */
const EXCLUDED_ACT=new Set(D.excluded_activities||[]);
/* Species belonging to a kind of place this map no longer draws. Fish and
   shellfish are here because aquaculture is: a fish farm with nothing else on
   it is already off the map, and leaving the species filter behind would offer
   a toggle that selects almost nothing. */
const EXCLUDED_SPECIES=new Set(['fish','crustacean']);
const actPresent=[...new Set((D.dict.activity||[]).flat())]
  .filter(a=>!EXCLUDED_ACT.has(a))
  .sort((a,b)=>{
    const ia=ACTIVITY_ORDER.indexOf(a), ib=ACTIVITY_ORDER.indexOf(b);
    return (ia<0?99:ia)-(ib<0?99:ib);
  });
if(actPresent.length){
  group('What happens here', actPresent.map(a=>({key:a,
    label:ACTIVITY_LABEL[a]||a, note:ACTIVITY_NOTE[a]||'',
    fate:ACTIVITY_FATE[a]})), active.activity,
    false, 'Each registry says what the place does, and whether the animals '+
    'there are killed. A facility can be listed under several of these, so the '+
    'percentages overlap and do not sum to 100.', true);
}

const order=Object.keys(SPECIES_LABEL);
const speciesPresent=[...new Set(D.dict.species.flat())]
  .filter(sp=>!EXCLUDED_SPECIES.has(sp))
  .sort((a,b)=>order.indexOf(a)-order.indexOf(b));
group('Species', speciesPresent.map(s=>({key:s,label:SPECIES_LABEL[s]||s,
  colour:D.colour[s]})).concat([{key:'_none',label:'Not stated',
  colour:D.colour._unstated}]), active.species, true,
  'Which animals a registry names. Most third-country listings name none.');
group('Slaughter activity',[{key:1,label:'Confirmed',note:SLAUGHTER_NOTE[1]},
  {key:0,label:'Listed as none',note:SLAUGHTER_NOTE[0]},
  {key:2,label:'Not stated',note:SLAUGHTER_NOTE[2]}], active.slaughter);
group('Registry',[...new Set(D.dict.source.flat())].sort().map(s=>({
  key:s,label:(D.sources_meta[s]&&D.sources_meta[s].short)||s,
  note:(D.sources_meta[s]&&D.sources_meta[s].name)||''})), active.source);

function updateTallies(){
  const t={};
  for(let i=0;i<N;i++){
    if(!VIS[i]) continue;
    const sp=D.dict.species[D.sp[i]].filter(s=>!EXCLUDED_SPECIES.has(s));
    if(sp.length===0) t['Species::_none']=(t['Species::_none']||0)+1;
    else sp.forEach(s=>{const k='Species::'+s;t[k]=(t[k]||0)+1;});
    if(D.dict.activity&&D.act){
      const ac=D.dict.activity[D.act[i]].filter(a=>!EXCLUDED_ACT.has(a));
      if(ac.length===0) t['What happens here::_none']=(t['What happens here::_none']||0)+1;
      else ac.forEach(a=>{const k='What happens here::'+a;t[k]=(t[k]||0)+1;});
    }
    const sk='Slaughter activity::'+D.sl[i]; t[sk]=(t[sk]||0)+1;
    D.dict.source[D.src[i]].forEach(s=>{const k='Registry::'+s;t[k]=(t[k]||0)+1;});
  }
  document.querySelectorAll('.tally').forEach(el=>{
    if(!el.dataset.k) return;      /* static counts, e.g. the CAFO overlay */
    const v=t[el.dataset.k];
    el.textContent = v ? v.toLocaleString()+' drawn' : 'none drawn';
  });
}

/* ---- where cultivated meat is banned ----------------------------------- */
/* Filled outlines rather than points, because the subject is a jurisdiction
   rather than a site. Drawn under everything else, since it is the ground a
   reader is standing on rather than a thing on the map. */
const LAWS = D.laws || null;
let SHOW_LAWS = false;

function lawShapes(){
  /* {status: [rings]} for every country and state named in the file. */
  const out = [];
  if(!LAWS) return out;
  const byIso = {};
  for(const c of OUT) if(c.iso3) byIso[c.iso3] = c.rings;
  for(const c of (LAWS.countries||[])){
    if(byIso[c.code]) out.push({status:c.status, rings:byIso[c.code]});
  }
  const byPostal = {};
  for(const s of (LAWS.us_shapes||[])){
    byPostal[s.code] = s.r.map(ring=>{
      const wx=new Float64Array(ring.length), wy=new Float64Array(ring.length);
      for(let i=0;i<ring.length;i++){
        wx[i]=(ring[i][0]+180)/360;
        const sn=Math.sin(ring[i][1]*Math.PI/180);
        wy[i]=0.5 - Math.log((1+sn)/(1-sn))/(4*Math.PI);
      }
      return {wx,wy};
    });
  }
  for(const s of (LAWS.us_states||[])){
    if(byPostal[s.code]) out.push({status:s.status, rings:byPostal[s.code]});
  }
  return out;
}
const LAW_SHAPES = LAWS ? lawShapes() : [];

const LawLayer = L.Layer.extend({
  onAdd(m){
    this._c = L.DomUtil.create('canvas','leaflet-zoom-animated');
    this._ctx = this._c.getContext('2d');
    m.getPanes().overlayPane.appendChild(this._c);
    m.on('moveend zoomend resize',this.draw,this);
    if(m.options.zoomAnimation) m.on('zoomanim',this._anim,this);
    this.draw();
  },
  _anim(e){
    const s=this._map.getZoomScale(e.zoom), o=this._map._latLngToNewLayerPoint(
      this._map.getBounds().getNorthWest(), e.zoom, e.center);
    L.DomUtil.setTransform(this._c,o,s);
  },
  draw(){
    const m=this._map; if(!m) return;
    const size=m.getSize(), dpr=window.devicePixelRatio||1;
    const tl=m.containerPointToLayerPoint([0,0]);
    L.DomUtil.setPosition(this._c,tl);
    this._c.width=size.x*dpr; this._c.height=size.y*dpr;
    this._c.style.width=size.x+'px'; this._c.style.height=size.y+'px';
    const ctx=this._ctx; ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,size.x,size.y);
    if(!SHOW_LAWS || !LAW_SHAPES.length) return;
    const z=m.getZoom(), S=256*Math.pow(2,z), o=m.getPixelOrigin();
    const offX=o.x+tl.x, offY=o.y+tl.y, w=size.x, h=size.y;
    for(const sh of LAW_SHAPES){
      const st=(LAWS.statuses||{})[sh.status];
      if(!st) continue;
      ctx.globalAlpha=.42; ctx.fillStyle=st.colour;
      ctx.beginPath(); tracePath(ctx, sh.rings, S, offX, offY, w, h);
      ctx.fill('evenodd');
      ctx.globalAlpha=.85; ctx.lineWidth=1; ctx.strokeStyle=st.colour;
      ctx.stroke();
    }
    ctx.globalAlpha=1;
  }
});
const lawLayer = LAWS ? new LawLayer() : null;

/* ---- Climate TRACE confined animal facilities -------------------------- */
/* Drawn on their own canvas, under the registry points and over the basemap,
   so nothing here can be mistaken for a licensed premises. Uniform radius on
   purpose: the archive carries an emissions value, and sizing a dot by it would
   say "this one matters more", which is not what a locations layer is for. */
const CAFO = D.cafo || null;
let SHOW_CAFO = false;

const CafoLayer = L.Layer.extend({
  onAdd(m){
    this._c = L.DomUtil.create('canvas','leaflet-zoom-animated');
    this._ctx = this._c.getContext('2d');
    m.getPanes().overlayPane.appendChild(this._c);
    m.on('moveend zoomend resize',this.draw,this);
    if(m.options.zoomAnimation) m.on('zoomanim',this._anim,this);
    this.draw();
  },
  _anim(e){
    const s=this._map.getZoomScale(e.zoom), o=this._map._latLngToNewLayerPoint(
      this._map.getBounds().getNorthWest(), e.zoom, e.center);
    L.DomUtil.setTransform(this._c,o,s);
  },
  draw(){
    const m=this._map; if(!m || !CAFO) return;
    const size=m.getSize(), dpr=window.devicePixelRatio||1;
    const tl=m.containerPointToLayerPoint([0,0]);
    L.DomUtil.setPosition(this._c,tl);
    this._c.width=size.x*dpr; this._c.height=size.y*dpr;
    this._c.style.width=size.x+'px'; this._c.style.height=size.y+'px';
    const ctx=this._ctx; ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,size.x,size.y);
    if(!SHOW_CAFO) return;

    const z=m.getZoom(), S=256*Math.pow(2,z), o=m.getPixelOrigin();
    const offX=o.x+tl.x, offY=o.y+tl.y, w=size.x, h=size.y;
    const r = z<5 ? 1.6 : (z<8 ? 2.4 : (z<11 ? 3.2 : 4.2));
    ctx.lineWidth = 1;
    for(let i=0;i<CAFO.n;i++){
      const wx=(CAFO.lon[i]+180)/360;
      const sn=Math.sin(CAFO.lat[i]*Math.PI/180);
      const wy=0.5 - Math.log((1+sn)/(1-sn))/(4*Math.PI);
      const x=wx*S-offX, y=wy*S-offY;
      if(x<-10||x>w+10||y<-10||y>h+10) continue;
      ctx.beginPath(); ctx.arc(x,y,r,0,TAU);
      if(CAFO.precise[i]){
        ctx.globalAlpha=.7; ctx.fillStyle='#7B6A4E'; ctx.fill();
      }else{
        /* Hollow: the source gave an area, not a position. */
        ctx.globalAlpha=.8; ctx.strokeStyle='#7B6A4E'; ctx.stroke();
      }
    }
    ctx.globalAlpha=1;
  }
});
const cafoLayer = CAFO ? new CafoLayer() : null;

if(CAFO){
  map.attributionControl.addAttribution(
    'Confined animal facilities: <a href="https://climatetrace.org/">Climate '+
    'TRACE</a>, CC BY 4.0 &mdash; modelled, not a permit register');
  (function(){
    const g=document.createElement('div'); g.className='grp';
    g.innerHTML='<h2>Confined animal facilities</h2>';
    const p=document.createElement('p'); p.className='lede';
    p.textContent='Climate TRACE model these from satellite imagery and census '+
      'data. Nothing here has necessarily been visited, licensed or confirmed '+
      'by any authority, which is what separates them from every other point '+
      'on this map.';
    g.appendChild(p);
    const l=document.createElement('label'); l.className='row';
    const cb=document.createElement('input'); cb.type='checkbox';
    cb.checked = true;                 /* on by default */
    SHOW_CAFO = true;
    cb.onchange=function(){ SHOW_CAFO=cb.checked; cafoLayer.draw(); };
    l.appendChild(cb);
    const body=document.createElement('div'); body.className='rowbody';
    const top=document.createElement('div'); top.className='rowtop';
    const t=document.createElement('span'); t.className='lbl';
    t.textContent='Show modelled facilities';
    const n=document.createElement('span'); n.className='tally';
    n.textContent=CAFO.n.toLocaleString()+' points';
    top.appendChild(t); top.appendChild(n); body.appendChild(top);
    const solid=CAFO.precise.reduce((a,b)=>a+b,0);
    const hollow=CAFO.n-solid;
    const h=document.createElement('span'); h.className='hint';
    h.innerHTML=(CAFO.periods.length===1
        ? 'One month of data, '+esc(CAFO.periods[0])+'. '
        : CAFO.periods.length>1
        ? esc(CAFO.periods.length)+' months folded to one point per facility, '+
          esc(CAFO.periods[0])+' to '+esc(CAFO.periods[CAFO.periods.length-1])+'. '
        : '')+
      (hollow
        ? '<span class="share">'+hollow.toLocaleString()+'</span> are drawn '+
          'hollow because the source gave an area rather than a position.'
        : 'Every one carries a modelled position rather than an area.');
    body.appendChild(h);
    const f=document.createElement('span'); f.className='fate';
    f.textContent='modelled, not licensed'; f.style.color='#8b8474';
    body.appendChild(f);
    l.appendChild(body); g.appendChild(l);
    F.appendChild(g);
  })();
}

/* ---- livestock density toggle ----------------------------------------- */
(function(){
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>Livestock density</h2>';
  const p=document.createElement('p'); p.className='lede';
  p.textContent='FAO model subnational census totals down onto a grid, so a '+
    'cell is an estimate of how many animals are in it, not a count of animals '+
    'seen there.';
  g.appendChild(p);

  const l=document.createElement('label'); l.className='row';
  const cb=document.createElement('input'); cb.type='checkbox';
  l.appendChild(cb);
  const body=document.createElement('div'); body.className='rowbody';
  const top=document.createElement('div'); top.className='rowtop';
  const t=document.createElement('span'); t.className='lbl';
  t.textContent='Show FAO livestock grid';
  top.appendChild(t); body.appendChild(top);
  const h=document.createElement('span'); h.className='hint';
  h.innerHTML='Animals per square kilometre, 2020. Compare within a region '+
    'rather than across the map: this projection stretches everything far from '+
    'the equator, so the same shade covers less real ground in Canada or '+
    'Siberia than it does in Kenya, and the north looks fuller than it is.';
  body.appendChild(h);
  const f=document.createElement('span'); f.className='fate';
  f.textContent='modelled, not counted'; f.style.color='#8b8474';
  body.appendChild(f);
  l.appendChild(body); g.appendChild(l);

  const stat=document.createElement('span'); stat.className='hint';
  stat.id='glwStatus'; body.appendChild(stat);

  cb.onchange=function(){ setLivestock(cb.checked); };
  cb.checked = true;                 /* on by default */
  F.appendChild(g);
  setLivestock(true);
})();

/* ---- the cultivated meat toggle ---------------------------------------- */
if(LAWS){
  (function(){
    const g=document.createElement('div'); g.className='grp';
    g.innerHTML='<h2>Cultivated meat bans</h2>';
    const p=document.createElement('p'); p.className='lede';
    p.textContent=LAWS.note||'';
    g.appendChild(p);

    const l=document.createElement('label'); l.className='row';
    const cb=document.createElement('input'); cb.type='checkbox';
    cb.onchange=function(){ SHOW_LAWS=cb.checked; lawLayer.draw(); };
    l.appendChild(cb);
    const body=document.createElement('div'); body.className='rowbody';
    const top=document.createElement('div'); top.className='rowtop';
    const t=document.createElement('span'); t.className='lbl';
    t.textContent='Shade the jurisdictions';
    const n=document.createElement('span'); n.className='tally';
    n.textContent=((LAWS.countries||[]).length+(LAWS.us_states||[]).length)+' laws';
    top.appendChild(t); top.appendChild(n); body.appendChild(top);
    const h=document.createElement('span'); h.className='hint';
    h.innerHTML='<button class="link" id="showLaws">What each one says</button>';
    body.appendChild(h); l.appendChild(body); g.appendChild(l);

    for(const [key,st] of Object.entries(LAWS.statuses||{})){
      const r=document.createElement('label'); r.className='row';
      r.style.cursor='default';
      const sp=document.createElement('span'); sp.style.cssText=
        'width:9px;height:9px;border-radius:2px;flex:none;margin-top:4px;'+
        'background:'+st.colour;
      r.appendChild(sp);
      const rb=document.createElement('div'); rb.className='rowbody';
      const rt=document.createElement('div'); rt.className='rowtop';
      const rl=document.createElement('span'); rl.className='lbl';
      rl.textContent=st.label; rt.appendChild(rl); rb.appendChild(rt);
      r.appendChild(rb); g.appendChild(r);
    }
    F.appendChild(g);

    document.getElementById('showLaws').onclick=function(){
      let h='<h3>Restrictions on cultivated meat</h3>';
      h+='<p class="where">'+esc(LAWS.note||'')+'</p><div class="prov kinds">';
      const block=(title,rows,nameOf)=>{
        if(!rows||!rows.length) return '';
        let s='<h4>'+esc(title)+'</h4><ul>';
        for(const r of rows){
          const st=(LAWS.statuses||{})[r.status]||{label:r.status};
          s+='<li><b>'+esc(nameOf(r))+'</b> &middot; '+esc(st.label)+'<br>'+
             esc(r.what||'');
          if(r.caveat) s+='<br><i>'+esc(r.caveat)+'</i>';
          s+='</li>';
        }
        return s+'</ul>';
      };
      h+=block('Countries', LAWS.countries, r=>r.code);
      h+=block('United States', LAWS.us_states, r=>r.code);
      if(LAWS.sources&&LAWS.sources.length){
        h+='<h4>Read from</h4><ul>';
        for(const s of LAWS.sources) h+='<li>'+esc(s)+'</li>';
        h+='</ul>';
      }
      h+='<p class="lede">Checked '+esc(LAWS.checked||'')+'.</p></div>';
      /* the drawer's panel, not this row's own div of the same name */
      document.getElementById('drawerBody').innerHTML=h;
      drawer.classList.add('open');
    };
  })();
}

/* ---- Counterglow, in the bottom half of the window --------------------- */
/* Their map is a register of reported farm locations; this one is a register
   of licences. Side by side is the only way to see where one has something the
   other does not. Loaded on first open, not on page load, so nobody pays for
   it who never asks. */
(function(){
  const dock=document.getElementById('dock');
  const frame=document.getElementById('dockFrame');
  const note=document.getElementById('dockNote');
  let loaded=false, timer=null;

  function open(){
    dock.classList.add('open'); dock.setAttribute('aria-hidden','false');
    if(loaded) return;
    loaded = true;
    frame.src = 'https://map.counterglow.org/';
    /* Counterglow refuses to be shown inside another page, and a browser
       reports that to the page as nothing at all -- no error, just an empty
       rectangle. Tried once anyway in case that changes; if the frame has not
       loaded shortly, the panel says so and offers the window instead. A popup
       is used rather than a tab so the two maps can sit side by side. */
    frame.addEventListener('load', function(){
      clearTimeout(timer); note.style.display='none';
    });
    timer = setTimeout(function(){
      frame.removeAttribute('src');
      note.innerHTML =
        '<span><b>Counterglow does not allow itself to be shown inside '+
        'another page.</b><br>It has to open in its own window, which can sit '+
        'beside this one.<br><br>'+
        '<button class="dock-open" id="dockPop">Open Counterglow in a '+
        'window</button></span>';
      const pop=document.getElementById('dockPop');
      if(pop) pop.onclick=function(){
        const w=Math.min(1100, screen.availWidth-40);
        const h=Math.min(760, screen.availHeight-80);
        window.open('https://map.counterglow.org/','counterglow',
          'width='+w+',height='+h+',left=20,top=40,noopener');
      };
    }, 3500);
  }
  function close(){
    dock.classList.remove('open'); dock.setAttribute('aria-hidden','true');
  }
  document.getElementById('dockClose').onclick = close;
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape') close();
  });

  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>Another map</h2>';
  const p=document.createElement('p'); p.className='lede';
  p.textContent='Counterglow maps animal farms reported by the public and by '+
    'campaigners \u2014 reports rather than licences, so it holds places no '+
    'register lists. Their site will not display inside another page, so it '+
    'opens in a window of its own.';
  g.appendChild(p);
  const l=document.createElement('label'); l.className='row';
  const b=document.createElement('button'); b.className='link';
  b.textContent='Open Counterglow';
  b.onclick=open;
  l.appendChild(b); g.appendChild(l);
  F.appendChild(g);
})();

/* ---- unplaced-facility layer toggle ----------------------------------- */
(function(){
  const total=Object.values(UNLOC_BY_C).reduce((a,b)=>a+b,0);
  if(!total) return;
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>Unplaced facilities</h2>';
  const p=document.createElement('p'); p.className='lede';
  p.textContent='Shade each country by how many of its facilities have a '+
    'registry listing but no address precise enough to draw. Country level '+
    'only \u2014 it says how many and where, not where in the country.';
  g.appendChild(p);
  const l=document.createElement('label'); l.className='row';
  const cb=document.createElement('input'); cb.type='checkbox';
  cb.onchange=function(){ SHOW_UNLOC=cb.checked; coarse.draw(); };
  l.appendChild(cb);
  const body=document.createElement('div'); body.className='rowbody';
  const top=document.createElement('div'); top.className='rowtop';
  const t=document.createElement('span'); t.className='lbl';
  t.textContent='Show as country shading';
  top.appendChild(t); body.appendChild(top);
  const h=document.createElement('span'); h.className='hint';
  h.innerHTML='<span class="share">'+total.toLocaleString()+'</span> facilities '+
    'with a registry listing but no address precise enough to place. None is '+
    'drawn &mdash; no dot, no circle, no town centre standing in for a street. '+
    '<button class="link" id="showUnplaced">See the list</button>';
  body.appendChild(h); l.appendChild(body); g.appendChild(l);
  F.appendChild(g);
})();

/* ---- unlocated ------------------------------------------------------- */
const u=D.unlocated||[];
/* The rail's last word, rather than a note about the dataset's gaps: what the
   map is a record of, and what its empty regions actually mean. */
document.getElementById('unlocatedNote').innerHTML =
  '<b>What this map is not.</b> These are registry listings, not a census. A '+
  'plant appears because some authority licensed it, and most authorities '+
  'license only what exports or what falls above a size threshold. Where the '+
  'map is empty, that is usually a gap in who keeps records rather than a '+
  'place where animals are not killed.';
if(u.length){
  document.getElementById('showUnplaced').onclick=function(){
    let h='<h3>Facilities without a usable location</h3>';
    h+='<p class="where">'+u.length.toLocaleString()+' records. Listed by a registry, '+
       'but with an address too coarse to geocode to a street or building. They '+
       'are not drawn anywhere on the map.</p><div class="prov kinds">';
    const byc=Object.entries(UNLOC_BY_C);
    if(byc.length){
      h+='<h4>By country</h4><ul>';
      for(const [c,n] of byc.slice(0,40))
        h+='<li><b>'+esc(c)+'</b> &middot; '+n.toLocaleString()+'</li>';
      if(byc.length>40) h+='<li>and '+(byc.length-40)+' more</li>';
      h+='</ul><h4>The records</h4>';
    }
    for(const r of u.slice(0,500)){
      h+='<div class="rec"><div class="who">'+esc(r.name)+'</div>'+
         '<div class="addr">'+esc([r.locality,r.country].filter(Boolean).join(', '))+
         ' &middot; '+esc(r.sources.join(', '))+'</div></div>';
    }
    if(u.length>500) h+='<p class="lede">Showing the first 500. The full set is in '+
      'facilities.json.</p>';
    h+='</div>';
    body.innerHTML=h; drawer.classList.add('open');
  };
}

document.getElementById('toggleRail').onclick=function(){
  document.getElementById('rail').classList.toggle('open');};
document.addEventListener('keydown',function(e){
  if(e.key==='Escape') drawer.classList.remove('open');});

/* The rail is assembled in whatever order these blocks happen to run, which is
   not the order it reads best in. Sorting once here, after every group exists,
   keeps the code grouped by subject and the panel grouped by use. */
(function(){
  /* What the map is about first; how it is drawn last. */
  const ORDER=['What happens here','Confined animal facilities',
    'Livestock density','Cultivated meat bans','Species','Slaughter activity',
    'Unplaced facilities','Registry','Another map','Basemap'];
  const groups=[...F.querySelectorAll('.grp')];
  groups.sort((a,b)=>{
    const ia=ORDER.indexOf(a.querySelector('h2').textContent);
    const ib=ORDER.indexOf(b.querySelector('h2').textContent);
    return (ia<0?99:ia)-(ib<0?99:ib);
  });
  for(const g of groups) F.appendChild(g);
})();

rebuildVis();
if(lawLayer) lawLayer.addTo(map);   /* under everything: it is ground, not a thing */
coarse.addTo(map);        /* under the points, over the basemap */
if(cafoLayer) cafoLayer.addTo(map);
layer.addTo(map);

/* Last, not first. setBasemap ends by redrawing the point layer, and a layer
   that has not joined the map yet has no map to draw against -- calling this
   earlier threw before the points were ever added, leaving the panel up and
   the map empty. */
setBasemap(D.plate ? 'atlas' : 'outlines');

})();
</script>
</body>
</html>
"""


def build(facilities, out_path: str, *, title: str, subtitle: str,
          sources_meta: dict | None = None,
          keep_post_mortem: bool = False, inline: bool = False) -> dict:
    """Write the atlas, and by default write its data beside it.

    The single-file build was right when the payload was a few megabytes. At
    thirteen it costs a phone a long blank screen before the first pin appears,
    because the browser must download and parse the whole document before it
    renders anything at all. Splitting the JSON out drops the page itself to
    around 40 KB, so the map frame and the panel are up immediately and the
    data arrives behind a loading line.

    inline=True restores the old behaviour. It is the build to use for anything
    that has to work from disk or travel as one file: fetch cannot read a
    sibling file over file://, so a split build opened by double-clicking shows
    the failure message rather than a map.
    """
    dropped = []
    if not keep_post_mortem:
        facilities, dropped = drop_excluded(facilities)
    out = Path(out_path)
    # The plate travels beside the page by default and inside it when inlined,
    # for the same reason the data does.
    plate_src = None
    if PLATE_PATH.exists():
        if inline:
            import base64
            plate_src = ("data:image/webp;base64,"
                         + base64.b64encode(PLATE_PATH.read_bytes()).decode())
        else:
            # The filename carries a hash of the image. Browsers and CDNs cache
            # a plate hard, so republishing under the same name leaves readers
            # looking at the old painting indefinitely; a new image is simply a
            # new address.
            import hashlib
            raw = PLATE_PATH.read_bytes()
            tag = hashlib.sha1(raw).hexdigest()[:8]
            plate_out = out.parent / f"atlas-plate-{tag}.webp"
            plate_out.write_bytes(raw)
            for old_plate in out.parent.glob("atlas-plate-*.webp"):
                if old_plate != plate_out:
                    old_plate.unlink()
            plate_src = plate_out.name
    payload_plate = ({"src": plate_src, "s": PLATE_SOUTH, "n": PLATE_NORTH,
                      "fin": PLATE_FADE_IN, "fout": PLATE_FADE_OUT}
                     if plate_src else None)

    payload = encode(facilities, sources_meta or {})
    payload["plate"] = payload_plate
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    data_path = data_gz = None

    if inline:
        html = TEMPLATE.replace("__DATA_SRC__", "").replace("__DATA__", blob)
    else:
        data_path = out.with_suffix(".data.json")
        data_path.write_text(blob, encoding="utf-8")
        data_gz = Path(str(data_path) + ".gz")
        with gzip.open(data_gz, "wt", encoding="utf-8") as fh:
            fh.write(blob)
        html = (TEMPLATE
                .replace("__DATA_SRC__", f' data-src="{data_path.name}"')
                .replace("__DATA__", ""))

    html = (html
            .replace("__TITLE__", title)
            .replace("__SUBTITLE_BLOCK__",
                     f'<p class="sub">{subtitle}</p>' if subtitle.strip() else ""))
    out.write_text(html, encoding="utf-8")

    size_mb = out.stat().st_size / 1e6
    result = {
        "path": out_path,
        "size_mb": round(size_mb, 2),
        "mapped": payload["n_mapped"],
        "unlocated": payload["n_unlocated"],
        "excluded_from_map": len(dropped),
        "by_country": dict(Counter(f.country_iso3 for f in facilities).most_common(25)),
    }
    if plate_src and not inline:
        result["plate"] = str(out.parent / plate_src)
    if data_path is not None:
        result["data_path"] = str(data_path)
        result["data_mb"] = round(data_path.stat().st_size / 1e6, 2)
        result["data_gz_mb"] = round(data_gz.stat().st_size / 1e6, 2)
        result["note"] = ("Split build: the page needs its .data.json beside it "
                          "and must be served over http. Use --inline for a "
                          "single self-contained file.")
    elif size_mb > 12:
        result["warning"] = ("Over 12 MB in one file. Drop --inline to split "
                             "the payload out.")
    return result
