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
    "porcine": "#a1808d",
    "poultry": "#7e8e6c",
    "ovine": "#6d8b93",
    "caprine": "#6d8b93",
    "equine": "#8b8474",
    "cervid": "#8b8474",
    "lagomorph": "#8b8474",
    "farmed_game": "#8b8474",
    "wild_game": "#8b8474",
    "other": "#8b8474",
    "_mixed": "#8f8d7e",
    "_unstated": "#5e6a66",
}

SPECIES_ORDER = ["bovine", "porcine", "poultry", "ovine", "caprine",
                 "equine", "cervid", "lagomorph", "farmed_game", "wild_game", "other"]


def _dot_colour(species: list[str]) -> str:
    if not species:
        return SPECIES_COLOUR["_unstated"]
    primary = [s for s in SPECIES_ORDER if s in species]
    if not primary:
        return SPECIES_COLOUR["_unstated"]
    if len(primary) > 2:
        return SPECIES_COLOUR["_mixed"]
    return SPECIES_COLOUR.get(primary[0], SPECIES_COLOUR["other"])


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
    "farm_honey", "farm_wool", "farm_skins",
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
        ci.append(idx(palette, _dot_colour(f.species)))
        ids.append([f"{m['source']}|{m.get('id_scheme') or ''}|{m.get('national_id') or ''}"
                    for m in f.members])

    def inv(d):
        return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1])]

    return {
        "shares": shares(facilities),
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
        "palette": inv(palette),
        "colour": {s: SPECIES_COLOUR[s] for s in SPECIES_ORDER}
                  | {"_mixed": SPECIES_COLOUR["_mixed"],
                     "_unstated": SPECIES_COLOUR["_unstated"]},
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
#kindsNote{padding:12px 20px;color:var(--dim);font-size:11.5px;line-height:1.55;
  border-top:1px solid var(--rule)}
.kinds h4{font-size:12px;color:var(--text);margin:16px 0 2px;font-weight:600}
.kinds p{color:var(--dim);font-size:12px;line-height:1.6;margin:0 0 4px}
.kinds ul{margin:2px 0 0;padding-left:17px;color:var(--dim);font-size:12px;
  line-height:1.6}
.kinds li b{color:var(--text);font-weight:500}
.tally{margin-left:auto;color:var(--dim);font-size:11px;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.grp .lede{color:var(--dim);font-size:11px;line-height:1.5;margin:-4px 0 8px}
.note{padding:12px 20px;color:var(--dim);font-size:11.5px;line-height:1.55;
  border-top:1px solid var(--rule)}
button.link{background:none;border:0;color:var(--live);font:inherit;font-size:12px;
  padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:2px}

#drawer{position:absolute;top:0;right:0;bottom:0;width:370px;z-index:610;
  background:var(--panel);border-left:1px solid var(--rule);
  transform:translateX(100%);transition:transform .22s ease;overflow-y:auto}
#drawer.open{transform:none}
@media (prefers-reduced-motion:reduce){#drawer{transition:none}}
#drawer .close{position:absolute;top:12px;right:14px;background:none;border:0;
  color:var(--dim);font-size:20px;line-height:1;cursor:pointer;padding:4px}
#drawer h3{font-family:var(--serif);font-weight:400;font-size:19px;line-height:1.25;
  margin:0;padding:22px 46px 6px 20px}
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
  <div id="kindsNote"></div>
  <div class="note" id="unlocatedNote"></div>
</aside>

<aside id="drawer" aria-live="polite">
  <button class="close" id="closeDrawer" aria-label="Close details">&times;</button>
  <div id="drawerBody"></div>
</aside>

<div id="hover"></div>
<div id="boot">Loading&#8230;</div>

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

const SPECIES_LABEL = {bovine:'Cattle',porcine:'Pigs',poultry:'Poultry',ovine:'Sheep',
  caprine:'Goats',equine:'Horses',cervid:'Deer and elk',lagomorph:'Rabbits',
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
  farm_dairy:'Cows confined for milk; calves and spent cows go to slaughter',
  farm_eggs:'Hens confined for eggs; male chicks and spent hens are killed',
  farm_poultry:'Birds confined for breeding or production; the register does not say which',
  transporter:'A haulier licensed to move live animals on journeys over eight hours. The address is a company office, not a place animals are kept',
  farm_wool:'Sheep confined for wool', farm_skins:'Animals confined for skin or fur',
  farm_honey:'Bees kept for honey',
  insect_rearing:'Bumble bees reared in environmental isolation, for pollination rather than honey',
  germinal_products:'Semen, ova or embryos taken from breeding animals. Some are centres where donor animals are kept, some are teams that travel to them', hatchery:'Chicks hatched and sorted by sex',
  saleyard:'Animals bought and sold, usually on the way to a kill floor',
  quarantine:'Animals held in compulsory isolation before they may move or enter a country',
  live_market:'Animals sold alive, often killed on site',
  holding_yard:'Animals held in transit', aquaculture:'Fish or shellfish farmed',
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
    'a cutting plant, a cold store, a haulier, a fish farm, a hatchery',
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
const GROUPS = D.palette.map(col=>({colour:col, idx:[]}));
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
      const sp = D.dict.species[D.sp[i]];
      ok = sp.length===0 ? active.species.has('_none')
                         : sp.some(s=>active.species.has(s));
    }
    if(ok){
      const ac = (D.dict.activity&&D.act) ? D.dict.activity[D.act[i]] : null;
      if(ac) ok = ac.length===0 ? active.activity.has('_none')
                                : ac.some(a=>active.activity.has(a));
    }
    if(ok) ok = D.dict.source[D.src[i]].some(s=>active.source.has(s));
    VIS[i] = ok?1:0;
  }
  updateTallies();
}

const map = L.map('map',{worldCopyJump:true,zoomControl:true,
  attributionControl:true}).setView([26,8],3);
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
    const m=this._map, size=m.getSize(), dpr=window.devicePixelRatio||1;
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
  {maxNativeZoom:19, maxZoom:22,
   attribution:'Imagery &copy; <a href="https://www.esri.com/">Esri</a>, Maxar, '+
     'Earthstar Geographics, USDA FSA, USGS, Aerogrid, IGN, IGP and the GIS '+
     'user community'});

/* ---- FAO Gridded Livestock of the World -------------------------------- */
/* A modelled raster, and the only layer on this map that is not a place.
   Subnational census totals downscaled to a grid by a Random Forest model, so a
   pixel is an estimate of animals in that cell rather than animals counted
   there. No owner, no company, no farm behind any pixel. It sits under the
   points, because the points are evidence and this is context.

   Two things about the published tile template are unverified, and both fail
   quietly rather than loudly. FAO's catalogue maps TileCol={y} and TileRow={x},
   which is the reverse of the usual convention -- get it wrong and every tile
   still returns 200, you just get a transposed world. And the mapset serves one
   species without saying which. So the toggle ships with an axis swap next to
   it: turn the layer on, look at a coastline, and if the pattern does not sit
   on the land, hit swap. */
const GLW_ATTRIB =
  'Livestock density: FAO, <a href="https://data.apps.fao.org/catalog/iso/'+
  '9d1e149b-d63f-4213-978b-317a8eb42d02">Gridded Livestock of the World 4</a> '+
  '(2020), CC BY 4.0 &mdash; modelled, not counted';
const GLW_BASE = 'https://data.apps.fao.org/map/wmts/wmts'+
  '?layer=fao-gismgr/GLW4-2020/mapsets/D-DA'+
  '&tilematrixset=EPSG:3857&Service=WMTS&request=GetTile&Version=1.0.0'+
  '&Format=image/png&layertype=Image';

let glwLayer = null, glwSwapped = false;

function glwUrl(swapped){
  /* As published: TileCol={y}, TileRow={x}. Swapped is the conventional
     reading. One of the two is right and a coastline says which. */
  return GLW_BASE + '&TileMatrix={z}' +
    (swapped ? '&TileCol={x}&TileRow={y}' : '&TileCol={y}&TileRow={x}');
}

function setLivestock(on, swapped){
  glwSwapped = (swapped === undefined) ? glwSwapped : swapped;
  if(glwLayer){ map.removeLayer(glwLayer); glwLayer = null; }
  if(!on) return;
  glwLayer = L.tileLayer(glwUrl(glwSwapped), {
    opacity: 0.65, maxNativeZoom: 10, maxZoom: 22,
    attribution: GLW_ATTRIB,
    /* Leaflet loads tiles as <img>, so no CORS header is needed to draw them.
       It would be needed only to read their pixels back out of a canvas. */
  });
  glwLayer.addTo(map);
  if(map.hasLayer(layer)) layer.bringToFront();
}

function setBasemap(kind){
  SATELLITE = (kind==='satellite');
  if(SATELLITE){
    if(map.hasLayer(outlineBase)) map.removeLayer(outlineBase);
    if(!map.hasLayer(imagery)) imagery.addTo(map);
  }else{
    if(map.hasLayer(imagery)) map.removeLayer(imagery);
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
    const m=this._map, size=m.getSize(), dpr=window.devicePixelRatio||1;
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
       dark, so the same alpha reads as a smudge. */
    if(SATELLITE){ alpha=Math.min(1,alpha+.2); r+=.3; }

    let count=0;
    const d=r*2, wrap = z<6 ? S : 0;   // second world copy near the dateline

    for(const g of GROUPS){
      const arr=g.idx, n=arr.length;
      ctx.fillStyle=g.colour; ctx.globalAlpha=alpha;
      if(useRect){
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
          ctx.globalAlpha=SATELLITE?.85:.7; ctx.lineWidth=1;
          ctx.strokeStyle=SATELLITE?'rgba(255,255,255,.75)':'rgba(23,29,27,.9)';
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
function group(title,items,set,swatches,lede){
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>'+title+'</h2>';
  if(lede){ const p=document.createElement('p'); p.className='lede';
    p.textContent=lede; g.appendChild(p); }
  for(const it of items){
    const l=document.createElement('label'); l.className='row';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=set.has(it.key);
    cb.onchange=function(){ if(cb.checked) set.add(it.key); else set.delete(it.key);
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
    if(pct!==undefined) bits.push('<span class="share">'+pct.toFixed(1)+'%</span> of all '+
      (D.n_all||0).toLocaleString());
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
  const opts=[
    {k:'outlines',label:'Country outlines',
     note:'Embedded in this file. No tile server, nothing to expire, works offline.'},
    {k:'satellite',label:'Satellite imagery',
     note:'Esri World Imagery, free and keyless. Loads tiles over the network; '+
          'deep zoom shows the sheds, lagoons and stockyards themselves.'}];
  for(const o of opts){
    const l=document.createElement('label'); l.className='row';
    const rb=document.createElement('input'); rb.type='radio'; rb.name='basemap';
    rb.checked=(o.k==='outlines');
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
const actPresent=[...new Set((D.dict.activity||[]).flat())]
  .sort((a,b)=>{
    const ia=ACTIVITY_ORDER.indexOf(a), ib=ACTIVITY_ORDER.indexOf(b);
    return (ia<0?99:ia)-(ib<0?99:ib);
  });
if(actPresent.length){
  group('What happens here', actPresent.map(a=>({key:a,
    label:ACTIVITY_LABEL[a]||a, note:ACTIVITY_NOTE[a]||'',
    fate:ACTIVITY_FATE[a]})), active.activity,
    false, 'What each registry says the place does, and whether the animals '+
    'there are killed. A facility can be listed under several of these, so the '+
    'percentages overlap and do not sum to 100.');
}

const order=Object.keys(SPECIES_LABEL);
const speciesPresent=[...new Set(D.dict.species.flat())]
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
    const sp=D.dict.species[D.sp[i]];
    if(sp.length===0) t['Species::_none']=(t['Species::_none']||0)+1;
    else sp.forEach(s=>{const k='Species::'+s;t[k]=(t[k]||0)+1;});
    if(D.dict.activity&&D.act){
      const ac=D.dict.activity[D.act[i]];
      if(ac.length===0) t['What happens here::_none']=(t['What happens here::_none']||0)+1;
      else ac.forEach(a=>{const k='What happens here::'+a;t[k]=(t[k]||0)+1;});
    }
    const sk='Slaughter activity::'+D.sl[i]; t[sk]=(t[sk]||0)+1;
    D.dict.source[D.src[i]].forEach(s=>{const k='Registry::'+s;t[k]=(t[k]||0)+1;});
  }
  document.querySelectorAll('.tally').forEach(el=>{
    const v=t[el.dataset.k];
    el.textContent = v ? v.toLocaleString()+' drawn' : 'none drawn';
  });
}

/* ---- livestock density toggle ----------------------------------------- */
(function(){
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>Livestock density</h2>';
  const p=document.createElement('p'); p.className='lede';
  p.textContent='The one layer here that is not a place. FAO model subnational '+
    'census totals down onto a grid, so a cell is an estimate of how many '+
    'animals are in it, not a count of animals seen there.';
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
  h.innerHTML='Head per km&sup2;, reference year 2020. FAO advise against '+
    'drawing this in lat/long: at this projection a northern cell covers far '+
    'less ground than an equatorial one, so Canada and Siberia read heavier '+
    'than they are against the tropics.';
  body.appendChild(h);
  const f=document.createElement('span'); f.className='fate';
  f.textContent='modelled, not counted'; f.style.color='#8b8474';
  body.appendChild(f);
  l.appendChild(body); g.appendChild(l);

  /* Only useful once the layer is on, so it stays hidden until then. */
  const swapRow=document.createElement('label'); swapRow.className='row';
  swapRow.style.display='none';
  const sw=document.createElement('input'); sw.type='checkbox';
  swapRow.appendChild(sw);
  const sbody=document.createElement('div'); sbody.className='rowbody';
  const stop=document.createElement('div'); stop.className='rowtop';
  const st=document.createElement('span'); st.className='lbl';
  st.textContent='Swap tile axes';
  stop.appendChild(st); sbody.appendChild(stop);
  const sh=document.createElement('span'); sh.className='hint';
  sh.textContent='If the grid does not sit on the coastlines, the row and '+
    'column parameters are the wrong way round. This flips them.';
  sbody.appendChild(sh); swapRow.appendChild(sbody); g.appendChild(swapRow);

  cb.onchange=function(){
    swapRow.style.display = cb.checked ? '' : 'none';
    setLivestock(cb.checked, sw.checked);
  };
  sw.onchange=function(){ if(cb.checked) setLivestock(true, sw.checked); };
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
  h.innerHTML='<span class="share">'+total.toLocaleString()+'</span> facilities, '+
    'deepest where the most are missing';
  body.appendChild(h); l.appendChild(body); g.appendChild(l);
  F.appendChild(g);
})();

/* ---- what kind of places these are ----------------------------------- */
/* The commonest way to misread this map is to take every dot as a kill floor.
   Most are not. The panel below says which are, which feed one, and which do
   not lead to a killing at all, using the registries' own categories rather
   than a judgement laid over them. */
(function(){
  const pct = k => { const v=SHARES['What happens here::'+k];
    return v===undefined ? null : v.toFixed(1)+'%'; };
  const kill = pct('slaughter');
  document.getElementById('kindsNote').innerHTML =
    'Not every dot is a slaughterhouse. '+
    (kill ? kill+' of the facilities here are places where animals are killed; '
          : '')+
    'the rest are where animals are raised, held or traded before they get '+
    'there. Plants that only handle animals already dead are not drawn. '+
    '<button class="link" id="showKinds">What each kind is</button>';

  const SECTIONS = [
   {h:'Where animals are killed',
    p:'The kill floor itself, and the markets where animals are sold alive and '+
      'killed on the spot or within the hour.',
    keys:['slaughter','live_market']},
   {h:'Where animals are held or moved on the way',
    p:'No killing happens at these, and almost every animal that passes through '+
      'one is on its way to a place where it does. The transporters are the odd '+
      'entry on this map: a licensed haulier is a company, and the address is an '+
      'office rather than somewhere animals are kept.',
    keys:['saleyard','holding_yard','quarantine','transporter']},
   {h:'Where animals are confined and raised',
    p:'Animals live here and are killed elsewhere. A dairy cow is slaughtered '+
      'when her milk yield falls, usually at five or six years against a '+
      'natural twenty; a laying hen when she moults; a wool sheep when her '+
      'fleece thins. Male chicks are killed at the hatchery within a day of '+
      'hatching because they will not lay. None of that happens at the address '+
      'on the map, which is why these are the largest share of it.',
    keys:['farm_meat','farm_dairy','farm_poultry','farm_eggs','farm_wool','farm_skins',
          'hatchery','aquaculture','farm_honey','insect_rearing',
          'germinal_products']},
   {h:'Where bodies are handled, on a site that also kills',
    p:'A cutting line or a cold store attached to a kill floor. Plants that '+
      'only handle animals already dead -- standalone cutting plants, '+
      'processors, cold stores, renderers -- are held off this map, because a '+
      'dot on them says nothing about where the animal died. They are still in '+
      'the published dataset.',
    keys:['cutting','processing','minced_meat','meat_preparations',
          'game_handling','cold_store','rendering','casings','egg_products']},
   {h:'Where animals are used other ways',
    p:'Killing is not the purpose here, but it is a common ending: laboratory '+
      'animals at the end of a study, racing animals when they stop winning, '+
      'surplus zoo animals, unsold breeding stock.',
    keys:['experimentation','zoo','wildlife','racing','rodeo','entertainment',
          'pet_breeder','pet_shop','agricultural_show']},
   {h:'What "listed as none" means',
    p:'A registry that names activities and does not name killing among them. '+
      'It is a statement, not a silence: the EU registers print a code for '+
      'every approved activity, so a plant listed CP and CS does cutting and '+
      'cold storage and would carry SH if it slaughtered. Most of this bucket '+
      'is the trade around the kill floor rather than anything humane \u2014 '+
      'cutting plants and processors taking carcasses in, hauliers moving live '+
      'animals to slaughter, fish farms and hatcheries raising stock that will '+
      'be killed somewhere else. The animals still die; the registry is saying '+
      'they do not die at this address.',
    keys:[]},
   {h:'Where the registry did not say',
    p:'The listing exists and the activity field is empty. Silence is not a '+
      'no, and these are not counted as anything.',
    keys:['unknown','_none']}];

  document.getElementById('showKinds').onclick=function(){
    let h='<h3>What kind of places these are</h3>'+
      '<p class="where">Every category below is the registry&rsquo;s own. '+
      'Percentages are the share of all '+(D.n_all||0).toLocaleString()+
      ' facilities, located or not; a facility can be listed under several, so '+
      'they overlap.</p><div class="prov kinds">';
    for(const s of SECTIONS){
      const rows=s.keys.filter(k=>pct(k)!==null);
      if(!rows.length && s.keys.length) continue;
      h+='<h4>'+esc(s.h)+'</h4><p>'+esc(s.p)+'</p>';
      if(!rows.length) continue;
      h+='<ul>';
      for(const k of rows){
        h+='<li><b>'+esc(ACTIVITY_LABEL[k]||k)+'</b> &middot; '+pct(k)+
           ' &middot; '+esc(ACTIVITY_NOTE[k]||'')+'</li>';
      }
      h+='</ul>';
    }
    h+='<h4>What this map is not</h4><p>These are registry listings, not a '+
       'census. A plant appears because some authority licensed it, and most '+
       'authorities license only what exports or what falls above a size '+
       'threshold. Where the map is empty, that is usually a gap in who keeps '+
       'records rather than a place where animals are not killed.</p>';
    h+='</div>';
    body.innerHTML=h; drawer.classList.add('open');
  };
})();

/* ---- unlocated ------------------------------------------------------- */
const u=D.unlocated||[];
document.getElementById('unlocatedNote').innerHTML =
  u.length
  ? u.length.toLocaleString()+' facilities have a registry listing but no address '+
    'precise enough to place. They are not on the map at all &mdash; no dot, no '+
    'circle, no town centre standing in for a street. They are listed here and '+
    'carried in full in the published dataset. '+
    '<button class="link" id="showUn">See the list</button>'
  : 'Every facility in this dataset has a located address.';
if(u.length){
  document.getElementById('showUn').onclick=function(){
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

rebuildVis();
coarse.addTo(map);        /* under the points, over the basemap */
layer.addTo(map);

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
    payload = encode(facilities, sources_meta or {})
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    out = Path(out_path)
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
