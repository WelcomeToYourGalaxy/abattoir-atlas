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
    warning. 61 KB of Natural Earth outlines is the whole background, and the
    file has no network dependency left except Leaflet itself.
    """
    f = Path(__file__).parent / "world_outlines.json"
    return json.loads(f.read_text()) if f.exists() else []


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

    lat, lon, name, c_i, sp_i, src_i, tier_i, sl, uid, ids, ci, prec = (
        [] for _ in range(12))

    for f in mappable:
        lat.append(round(f.lat * 1e5))
        lon.append(round(f.lon * 1e5))
        name.append(f.name)
        c_i.append(idx(countries, f.country_iso3 or "—"))
        sp_i.append(idx(species_combos, tuple(f.species)))
        src_i.append(idx(source_combos, tuple(sorted({m["source"] for m in f.members}))))
        tier_i.append(idx(tier_combos, tuple(f.match_tiers)))
        sl.append({True: 1, False: 0, None: 2}[f.slaughter])
        uid.append(f.uid)
        ci.append(idx(palette, _dot_colour(f.species)))
        # 1 = located to a street or building, 0 = located to the town only.
        prec.append(1 if f.precise else 0)
        ids.append([f"{m['source']}|{m.get('id_scheme') or ''}|{m.get('national_id') or ''}"
                    for m in f.members])

    def inv(d):
        return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1])]

    return {
        "generated": date.today().isoformat(),
        "n_mapped": len(mappable),
        "n_unlocated": len(unlocated),
        "dict": {
            "country": inv(countries),
            "species": [list(t) for t in inv(species_combos)],
            "source": [list(t) for t in inv(source_combos)],
            "tier": [list(t) for t in inv(tier_combos)],
        },
        "palette": inv(palette),
        "colour": {s: SPECIES_COLOUR[s] for s in SPECIES_ORDER}
                  | {"_mixed": SPECIES_COLOUR["_mixed"],
                     "_unstated": SPECIES_COLOUR["_unstated"]},
        "lat": lat, "lon": lon, "name": name,
        "c": c_i, "sp": sp_i, "src": src_i, "tier": tier_i, "sl": sl,
        "ci": ci, "prec": prec, "uid": uid, "ids": ids,
        "n_precise": sum(prec),
        "n_approx": len(prec) - sum(prec),
        "unlocated": [{"name": f.name, "country": f.country_iso3,
                       "locality": f.locality, "uid": f.uid,
                       "sources": sorted({m["source"] for m in f.members})}
                      for f in unlocated],
        "sources_meta": sources_meta,
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
label.row{display:flex;align-items:center;gap:9px;padding:4px 0;cursor:pointer;
  font-size:13px;line-height:1.3}
label.row input{accent-color:var(--live);margin:0;flex:none}
.swatch{width:9px;height:9px;border-radius:50%;flex:none}
.tally{margin-left:auto;color:var(--dim);font-size:11.5px;
  font-variant-numeric:tabular-nums}
.legend{margin-top:11px;font-size:11.5px;color:var(--dim);line-height:1.5}
.legend span{display:flex;align-items:center;gap:8px;margin-top:4px}
.legend i{width:11px;height:11px;border-radius:50%;flex:none;font-style:normal}
.legend i.solid{background:var(--dim)}
.legend i.hollow{border:1.4px solid var(--dim)}
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
  <div class="count"><b id="shown">0</b><span id="shownNote">facilities in view</span>
    <div class="legend" id="legend"></div>
  </div>
  <div id="filters"></div>
  <div class="note" id="unlocatedNote"></div>
</aside>

<aside id="drawer" aria-live="polite">
  <button class="close" id="closeDrawer" aria-label="Close details">&times;</button>
  <div id="drawerBody"></div>
</aside>

<div id="hover"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script id="atlas-data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('atlas-data').textContent);
const N = D.lat.length, TAU = Math.PI*2;

const SPECIES_LABEL = {bovine:'Cattle',porcine:'Pigs',poultry:'Poultry',ovine:'Sheep',
  caprine:'Goats',equine:'Horses',cervid:'Deer and elk',lagomorph:'Rabbits',
  farmed_game:'Farmed game',wild_game:'Wild game',other:'Other'};
const SLAUGHTER_LABEL = {1:'Slaughter confirmed by a registry',
  0:'Registry lists no slaughter activity', 2:'No registry stated either way'};

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
/* Two draw passes per colour: filled dots for facilities located to a street
   or building, hollow rings for those located only to their town. The ring is
   not decoration -- it is the map refusing to claim a precision the register
   did not publish. */
const GROUPS = D.palette.map(col=>({colour:col, exact:[], approx:[]}));
for(let i=0;i<N;i++) (D.prec[i] ? GROUPS[D.ci[i]].exact : GROUPS[D.ci[i]].approx).push(i);
for(const g of GROUPS){ g.exact = Int32Array.from(g.exact);
                        g.approx = Int32Array.from(g.approx); }

/* ---- filter state; VIS is rebuilt only when a filter changes ---------- */
const VIS = new Uint8Array(N);
const active = {species:new Set(), slaughter:new Set([0,1,2]), source:new Set(),
                precision:new Set([0,1])};
D.dict.species.forEach(c=>c.forEach(s=>active.species.add(s)));
active.species.add('_none');
D.dict.source.forEach(c=>c.forEach(s=>active.source.add(s)));

function rebuildVis(){
  for(let i=0;i<N;i++){
    let ok = active.precision.has(D.prec[i]) && active.slaughter.has(D.sl[i]);
    if(ok){
      const sp = D.dict.species[D.sp[i]];
      ok = sp.length===0 ? active.species.has('_none')
                         : sp.some(s=>active.species.has(s));
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
const OUT = (D.outlines||[]).map(country =>
  country.map(ring => {
    const wx = new Float64Array(ring.length), wy = new Float64Array(ring.length);
    for(let i=0;i<ring.length;i++){
      wx[i] = (ring[i][0]+180)/360;
      const s = Math.sin(ring[i][1]*Math.PI/180);
      wy[i] = 0.5 - Math.log((1+s)/(1-s))/(4*Math.PI);
    }
    return {wx, wy};
  }));

const Base = L.Layer.extend({
  onAdd(m){
    this._c = L.DomUtil.create('canvas','leaflet-zoom-animated');
    this._ctx = this._c.getContext('2d');
    m.getPanes().tilePane.appendChild(this._c);
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
    const ctx=this._ctx; ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,size.x,size.y);

    const z=m.getZoom(), S=256*Math.pow(2,z), o=m.getPixelOrigin();
    const offX=o.x+tl.x, offY=o.y+tl.y, w=size.x, h=size.y;

    ctx.fillStyle='#202825';               /* land */
    ctx.strokeStyle='rgba(214,211,200,.17)';
    ctx.lineWidth = z < 4 ? 0.6 : 0.9;
    ctx.beginPath();
    for(const country of OUT){
      for(const ring of country){
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
    ctx.fill('evenodd');
    ctx.stroke();
  }
});
new Base().addTo(map);

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

    let count=0;
    const d=r*2, wrap = z<6 ? S : 0;   // second world copy near the dateline

    for(const g of GROUPS){
      /* town-level: hollow ring, slightly larger and fainter */
      const ar=g.approx, an=ar.length;
      if(an){
        const rr = r + (useRect ? 0.6 : 1.0), dd = rr*2;
        ctx.globalAlpha = alpha*0.75;
        ctx.strokeStyle = g.colour;
        ctx.lineWidth = useRect ? 1 : 1.3;
        ctx.beginPath();
        for(let k=0;k<an;k++){
          const i=ar[k]; if(!VIS[i]) continue;
          let x=WX[i]*S-offX;
          if(wrap){ if(x<-dd) x+=wrap; else if(x>w+dd) x-=wrap; }
          if(x<-dd||x>w+dd) continue;
          const y=WY[i]*S-offY;
          if(y<-dd||y>h+dd) continue;
          ctx.moveTo(x+rr,y); ctx.arc(x,y,rr,0,TAU);
          count++;
        }
        ctx.stroke();
      }

      /* street or building: filled dot */
      const arr=g.exact, n=arr.length;
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
          ctx.globalAlpha=.7; ctx.lineWidth=1;
          ctx.strokeStyle='rgba(23,29,27,.9)'; ctx.stroke();
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
     (srcs.length===1?' registry':' registries')+'</span></div>';
  h+='<div class="fact"><i>Location</i><span>'+
     (D.prec[i] ? 'Located to a street or building'
                : 'Located to this town only \u2014 the register gave no usable '+
                  'street address, so this marker is the settlement, not the site')+
     '</span></div></div>';
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
function group(title,items,set,swatches){
  const g=document.createElement('div'); g.className='grp';
  g.innerHTML='<h2>'+title+'</h2>';
  for(const it of items){
    const l=document.createElement('label'); l.className='row';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=set.has(it.key);
    cb.onchange=function(){ if(cb.checked) set.add(it.key); else set.delete(it.key);
      rebuildVis(); layer.draw(); };
    l.appendChild(cb);
    if(swatches&&it.colour){const s=document.createElement('span');
      s.className='swatch'; s.style.background=it.colour; l.appendChild(s);}
    const t=document.createElement('span'); t.textContent=it.label; l.appendChild(t);
    const n=document.createElement('span'); n.className='tally';
    n.dataset.k=title+'::'+it.key; l.appendChild(n);
    g.appendChild(l);
  }
  F.appendChild(g);
}
const order=Object.keys(SPECIES_LABEL);
const speciesPresent=[...new Set(D.dict.species.flat())]
  .sort((a,b)=>order.indexOf(a)-order.indexOf(b));
group('Species', speciesPresent.map(s=>({key:s,label:SPECIES_LABEL[s]||s,
  colour:D.colour[s]})).concat([{key:'_none',label:'Not stated',
  colour:D.colour._unstated}]), active.species, true);
group('Slaughter activity',[{key:1,label:'Confirmed'},
  {key:0,label:'Listed as none'},{key:2,label:'Not stated'}], active.slaughter);
group('Location precision',[
  {key:1,label:'Street or building'},
  {key:0,label:'Town only'}], active.precision);
group('Registry',[...new Set(D.dict.source.flat())].sort().map(s=>({
  key:s,label:(D.sources_meta[s]&&D.sources_meta[s].short)||s})), active.source);

function updateTallies(){
  const t={};
  for(let i=0;i<N;i++){
    if(!VIS[i]) continue;
    const sp=D.dict.species[D.sp[i]];
    if(sp.length===0) t['Species::_none']=(t['Species::_none']||0)+1;
    else sp.forEach(s=>{const k='Species::'+s;t[k]=(t[k]||0)+1;});
    const sk='Slaughter activity::'+D.sl[i]; t[sk]=(t[sk]||0)+1;
    const pk='Location precision::'+D.prec[i]; t[pk]=(t[pk]||0)+1;
    D.dict.source[D.src[i]].forEach(s=>{const k='Registry::'+s;t[k]=(t[k]||0)+1;});
  }
  document.querySelectorAll('.tally').forEach(el=>{
    const v=t[el.dataset.k]; el.textContent=v?v.toLocaleString():'';
  });
}

/* ---- unlocated ------------------------------------------------------- */
const u=D.unlocated||[];
document.getElementById('unlocatedNote').innerHTML =
  u.length
  ? u.length.toLocaleString()+' facilities have a registry listing but nothing '+
    'that places them even to a town. They are not drawn rather than pinned to a '+
    'country centroid. <button class="link" id="showUn">See the list</button>'
  : 'Every facility in this dataset has a located address.';
if(u.length){
  document.getElementById('showUn').onclick=function(){
    let h='<h3>Facilities without a usable location</h3>';
    h+='<p class="where">'+u.length.toLocaleString()+' records. Listed by a registry, '+
       'but with an address too coarse to geocode to a street or building.</p>'+
       '<div class="prov">';
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

document.getElementById('legend').innerHTML =
  '<span><i class="solid"></i>located to a street or building ('+
  (D.n_precise||0).toLocaleString()+')</span>'+
  '<span><i class="hollow"></i>located to the town only ('+
  (D.n_approx||0).toLocaleString()+')</span>';

document.getElementById('toggleRail').onclick=function(){
  document.getElementById('rail').classList.toggle('open');};
document.addEventListener('keydown',function(e){
  if(e.key==='Escape') drawer.classList.remove('open');});

rebuildVis();
layer.addTo(map);
</script>
</body>
</html>
"""


def build(facilities, out_path: str, *, title: str, subtitle: str,
          sources_meta: dict | None = None) -> dict:
    payload = encode(facilities, sources_meta or {})
    html = (TEMPLATE
            .replace("__TITLE__", title)
            .replace("__SUBTITLE_BLOCK__",
                     f'<p class="sub">{subtitle}</p>' if subtitle.strip() else "")
            .replace("__DATA__", json.dumps(payload, separators=(",", ":"),
                                            ensure_ascii=False)))
    Path(out_path).write_text(html, encoding="utf-8")

    size_mb = Path(out_path).stat().st_size / 1e6
    return {
        "path": out_path,
        "size_mb": round(size_mb, 2),
        "mapped": payload["n_mapped"],
        "unlocated": payload["n_unlocated"],
        "by_country": dict(Counter(f.country_iso3 for f in facilities).most_common(25)),
        "warning": (
            "Over 12 MB. Most shared hosts will serve this, but Weebly's embed "
            "path may not. Split the payload out if so."
            if size_mb > 12 else None
        ),
    }
