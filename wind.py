#!/usr/bin/env python3
"""
Stadium wind profile: how much a park's wind moves a fly ball, by direction
and by where in the outfield it was hit.

Method, following the convention these charts use:

  1. Build a league-wide baseline for how far a fly ball *should* carry, as a
     function of exit velocity and launch angle. Every ball is compared to
     other balls struck the same way, so the baseline carries no park bias.
  2. Keep balls hit above 25 degrees whose baseline is 300+ feet — the ones
     with enough hang time for wind to act on.
  3. Each ball's residual is its actual distance against that baseline, as a
     percentage. Average the residuals inside every
     (park x wind direction x spray angle) cell.

A positive cell means fly balls of that description carried *farther* at that
park than the same batted ball does league-wide.

    python3 wind.py                 # writes index.html
    python3 wind.py --min-cell 60   # stricter sample-size floor
"""

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

CACHE = Path(__file__).parent / ".cache"

# Home plate in Statcast's coordinate space, and the scale that turns the
# hit-chart pixels into degrees off dead center.
PLATE_X, PLATE_Y = 125.42, 198.27

# Six equal 15-degree slices from the left-field line to the right-field line.
SPRAY_EDGES = [-45, -30, -15, 0, 15, 30, 45]

# MLB's own wind labels, folded into the rows the chart shows. Crosswinds and
# calm are pooled: neither pushes a ball out or knocks it down.
WIND_ROWS = [
    ("out_cf", "Out to Center", {"Out To CF"}),
    ("out_lf", "Out to Left", {"Out To LF"}),
    ("out_rf", "Out to Right", {"Out To RF"}),
    ("light", "Light / Sideways", {"L To R", "R To L", "None", "Varies", ""}),
    ("in_lf", "In from Left", {"In From LF"}),
    ("in_cf", "In from Center", {"In From CF"}),
    ("in_rf", "In from Right", {"In From RF"}),
]
CALM_MPH = 5  # at or below this, direction is noise regardless of the label


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def spray_angle(hx, hy):
    """Degrees off dead center: negative pulls toward left field."""
    if hx is None or hy is None:
        return None
    return math.degrees(math.atan2(hx - PLATE_X, PLATE_Y - hy))


def spray_bucket(angle):
    if angle is None:
        return None
    a = max(-44.999, min(44.999, angle))  # foul-line outliers clamp to the corners
    for i in range(6):
        if SPRAY_EDGES[i] <= a < SPRAY_EDGES[i + 1]:
            return i
    return None


def parse_wind(s):
    """'13 mph, Out To LF' -> (13, 'Out To LF')."""
    mph, _, rest = (s or "").partition("mph")
    speed = fnum(mph.strip()) or 0.0
    return speed, rest.lstrip(", ").strip()


def wind_row(speed, label):
    if speed <= CALM_MPH:
        return "light"
    for key, _lab, labels in WIND_ROWS:
        if label in labels:
            return key
    return "light"


def load_wind():
    games = {}
    for p in sorted(CACHE.glob("wind_*.json")):
        games.update(json.loads(p.read_text()))
    return games


def load_balls():
    for p in sorted(CACHE.glob("sc_*.csv")):
        with p.open() as fh:
            yield from csv.DictReader(fh)


def build_baseline(rows, ev_bin=2.0, la_bin=2.0, min_n=25):
    """Mean distance by (exit velo, launch angle) cell, league-wide."""
    acc = defaultdict(lambda: [0.0, 0])
    for ev, la, dist in rows:
        k = (int(ev // ev_bin), int(la // la_bin))
        acc[k][0] += dist
        acc[k][1] += 1
    return {k: t / n for k, (t, n) in acc.items() if n >= min_n}, ev_bin, la_bin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cell", type=int, default=40,
                    help="fly balls needed before a cell is shown")
    ap.add_argument("--min-park", type=int, default=500)
    ap.add_argument("--out", default="index.html")
    a = ap.parse_args()

    winds = load_wind()
    print(f"{len(winds)} games with wind", flush=True)

    # Pass 1: keep the usable batted balls and learn the baseline from them.
    kept, seasons = [], set()
    for r in load_balls():
        ev, la = fnum(r["launch_speed"]), fnum(r["launch_angle"])
        dist = fnum(r["hit_distance_sc"])
        ang = spray_angle(fnum(r["hc_x"]), fnum(r["hc_y"]))
        g = winds.get(r["game_pk"])
        if None in (ev, la, dist, ang) or not g or la <= 25:
            continue
        b = spray_bucket(ang)
        if b is None:
            continue
        kept.append((ev, la, dist, b, r["game_pk"], r["game_year"]))
        seasons.add(r["game_year"])
    print(f"{len(kept)} fly balls above 25 degrees with wind + coordinates", flush=True)

    base, ev_bin, la_bin = build_baseline([(k[0], k[1], k[2]) for k in kept])
    print(f"baseline cells: {len(base)}", flush=True)

    # Pass 2: residual vs baseline, bucketed by park / wind / spray / season.
    # Season is kept in the key so the page can re-aggregate over any range the
    # reader picks — a wider window fills sparse cells, a narrow one tracks a
    # park's current configuration.
    cells = defaultdict(lambda: [0.0, 0])
    park_n = defaultdict(int)
    park_meta = {}
    used = 0
    for ev, la, dist, b, gpk, yr in kept:
        exp = base.get((int(ev // ev_bin), int(la // la_bin)))
        if not exp or exp < 300:
            continue  # only balls the model says should carry 300+
        g = winds[gpk]
        vid = g.get("venue_id")
        if not vid:
            continue
        speed, label = parse_wind(g["wind"])
        row = wind_row(speed, label)
        cells[(vid, row, b, yr)][0] += (dist - exp) / exp * 100.0
        cells[(vid, row, b, yr)][1] += 1
        park_n[vid] += 1
        park_meta.setdefault(vid, g.get("venue", ""))
        used += 1
    print(f"{used} balls cleared the 300-foot baseline filter", flush=True)

    years = sorted(seasons)
    parks = []
    for vid, n in sorted(park_n.items(), key=lambda kv: -kv[1]):
        if n < a.min_park:
            continue
        # rows[wind][spray] = [[sum, count] per season], rounded to keep the
        # payload small; the page divides after summing its chosen years.
        rows = {}
        for key, _lab, _s in WIND_ROWS:
            rows[key] = [
                [[round(cells[(vid, key, b, y)][0], 1), cells[(vid, key, b, y)][1]]
                 if (vid, key, b, y) in cells else [0, 0] for y in years]
                for b in range(6)
            ]
        parks.append({"id": vid, "name": park_meta[vid], "n": n, "rows": rows})
    print(f"{len(parks)} parks with >= {a.min_park} qualifying fly balls", flush=True)

    payload = {
        "parks": parks,
        "winds": [[k, lab] for k, lab, _ in WIND_ROWS],
        "seasons": years,
        "built": date.today().isoformat(),
        "minCell": a.min_cell,
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    Path(a.out).write_text(html, encoding="utf-8")
    print(f"wrote {a.out} ({Path(a.out).stat().st_size // 1024} KB)")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stadium Wind Profile</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto}
header{padding:20px 20px 12px}
.eyebrow{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--amber)}
h1{margin:2px 0 0;font-size:27px;font-weight:800;letter-spacing:-.035em;line-height:1.05}
.meta{font-family:var(--mono);font-size:10px;color:var(--faint);margin-top:6px;letter-spacing:.03em}
.controls{padding:8px 20px 14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.lbl{font-family:var(--mono);font-size:10px;color:var(--faint);letter-spacing:.05em}
select{height:30px;font-family:var(--mono);font-size:12px;color:var(--text);background:var(--panel);
  border:1px solid var(--line);border-radius:6px;padding:0 8px;max-width:100%}
select:focus{outline:1px solid var(--amberDim)}
.pill{padding:6px 12px;font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.03em;
  color:var(--amber);background:var(--panel);border:1px solid var(--amberDim);border-radius:100px;cursor:pointer}
.pill:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
body.capturing .pill{visibility:hidden}
.board{padding:0 20px 20px;display:flex;flex-direction:column;gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.cap{display:flex;align-items:baseline;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.cap b{font-size:15px;font-weight:700;letter-spacing:-.01em}
.cap span{font-family:var(--mono);font-size:10px;color:var(--faint)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:640px}
th,td{padding:6px 8px;text-align:center;font-family:var(--mono);font-size:11px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
thead th{color:var(--amber);font-size:9.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;border-bottom:1px solid var(--line)}
thead th.grp{color:var(--dim);border-bottom:none;padding-bottom:2px}
tbody td:first-child,tbody th:first-child{text-align:left;color:var(--text);font-weight:600;
  font-size:11px;position:sticky;left:0;background:var(--panel)}
tbody tr+tr td,tbody tr+tr th{border-top:1px solid rgba(53,74,148,.35)}
td.n{color:var(--dim)}
td.v{font-weight:700;color:#08122E}
td.thin{color:var(--faint);font-style:italic;font-weight:400}
.sep{border-left:1px solid var(--line2)}
footer{padding:14px 20px 24px;font-family:var(--mono);font-size:10px;color:var(--faint);line-height:1.7}
footer b{color:var(--dim);font-weight:600}
.key{display:inline-flex;align-items:center;gap:4px;margin-right:14px}
.key i{width:16px;height:11px;display:inline-block;border-radius:2px}
</style></head><body><div class="wrap">
<header>
  <div class="eyebrow">MLB · BATTED BALL CARRY</div>
  <h1>Stadium Wind Profile</h1>
  <div class="meta" id="meta"></div>
</header>
<div class="controls">
  <span class="lbl">COMPARE</span>
  <select id="a"></select>
  <span class="lbl">VS</span>
  <select id="b"></select>
  <span class="lbl" style="margin-left:6px">SEASONS</span>
  <select id="from"></select>
  <span class="lbl">TO</span>
  <select id="to"></select>
  <button class="pill" id="shot" type="button">Save image</button>
</div>
<div class="board" id="board"></div>
<footer>
  <div style="margin-bottom:6px">
    <span class="key"><i style="background:#1F9D55"></i>carries farther</span>
    <span class="key"><i style="background:#123A6B"></i>neutral</span>
    <span class="key"><i style="background:#C2334D"></i>carries shorter</span>
  </div>
  Fly balls hit above <b>25&deg;</b> whose launch conditions project to <b>300+ feet</b>.
  Each number is that park's average carry against the <b>MLB baseline for the same exit
  velocity and launch angle</b>, in percent &mdash; so +3% means those fly balls travelled
  three percent farther than an identically struck ball does league-wide.<br>
  Columns run from the left-field line to the right-field line. Winds of
  <b>5 mph or less</b> are pooled into Light / Sideways. Cells with fewer than
  <b id="mc"></b> fly balls are left blank. Data: MLB Stats API &amp; Baseball Savant.
</footer></div>
<script>
const D = __DATA__;
const GROUPS = [["Left Field",2],["Center Field",2],["Right Field",2]];

function color(v){
  if(v===null||v===undefined) return "";
  const t=Math.max(-1,Math.min(1,v/5));           // saturate around +/-5%
  const a=Math.abs(t);
  const lo=[18,58,107], hiG=[31,157,85], hiR=[194,51,77];
  const hi=t>=0?hiG:hiR;
  const c=lo.map((x,i)=>Math.round(x+(hi[i]-x)*Math.pow(a,.75)));
  return `background:rgb(${c.join(",")})`;
}
function fmt(v){ return v===null||v===undefined ? "" : (v>0?"+":"")+v.toFixed(1)+"%"; }

// Sum the per-season [sum,count] pairs inside the selected window.
function agg(pairs, lo, hi){
  let s=0,n=0;
  for(let i=lo;i<=hi;i++){ s+=pairs[i][0]; n+=pairs[i][1]; }
  return [s,n];
}

function table(p, lo, hi){
  const head1 = GROUPS.map(([g,n],gi)=>
    `<th class="grp ${gi?'sep':''}" colspan="${n}">${g}</th>`).join("");
  const head2 = GROUPS.map(([,n],gi)=>
    Array.from({length:n},(_,i)=>`<th class="${gi&&!i?'sep':''}"></th>`).join("")).join("");
  let parkN=0;
  const body = D.winds.map(([k,lab])=>{
    const buckets=(p.rows[k]||[]).map(pairs=>agg(pairs,lo,hi));
    const rowN=buckets.reduce((t,[,n])=>t+n,0);
    parkN+=rowN;
    // A row is only worth drawing if some individual cell clears the bar —
    // otherwise it reads as an unexplained blank band.
    const shown = buckets.filter(([,n])=>n>=D.minCell).length;
    const tds = shown === 0
      ? `<td class="thin" colspan="6">not enough data</td>`
      : buckets.map(([s,n],i)=>{
          const v = n>=D.minCell ? s/n : null;
          return `<td class="${v===null?'':'v'} ${i&&i%2===0?'sep':''}" style="${color(v)}">${fmt(v)}</td>`;
        }).join("");
    return `<tr><th>${lab}</th><td class="n">${rowN.toLocaleString()}</td>${tds}</tr>`;
  }).join("");
  return `<div class="card">
    <div class="cap"><b>${p.name}</b><span>${parkN.toLocaleString()} qualifying fly balls</span></div>
    <div class="scroll"><table>
      <thead>
        <tr><th></th><th></th>${head1}</tr>
        <tr><th style="text-align:left">Wind Direction</th><th>Fly Balls</th>${head2}</tr>
      </thead><tbody>${body}</tbody>
    </table></div></div>`;
}

function render(){
  let lo=+document.getElementById("from").value, hi=+document.getElementById("to").value;
  if(lo>hi){ [lo,hi]=[hi,lo]; }
  const ida=+document.getElementById("a").value, idb=+document.getElementById("b").value;
  const pa=D.parks.find(p=>p.id===ida), pb=D.parks.find(p=>p.id===idb);
  document.getElementById("board").innerHTML=
    [pa,pb].filter(Boolean).map(p=>table(p,lo,hi)).join("");
  document.getElementById("meta").textContent =
    `${D.seasons[lo]}–${D.seasons[hi]} regular seasons · ${D.parks.length} parks · built ${D.built}`;
}

document.getElementById("mc").textContent = D.minCell;
["from","to"].forEach((id,i)=>{
  const s=document.getElementById(id);
  s.innerHTML=D.seasons.map((y,ix)=>`<option value="${ix}">${y}</option>`).join("");
  s.value = i===0 ? 0 : D.seasons.length-1;
  s.onchange=render;
});

const sorted=[...D.parks].sort((x,y)=>x.name.localeCompare(y.name));
["a","b"].forEach((id,i)=>{
  const s=document.getElementById(id);
  s.innerHTML=sorted.map(p=>`<option value="${p.id}">${p.name}</option>`).join("");
  const pick = i===0 ? sorted.find(p=>/Wrigley/.test(p.name)) : sorted.find(p=>/Coors/.test(p.name));
  s.value = (pick||sorted[i]||sorted[0]).id;
  s.onchange=render;
});

document.getElementById("shot").onclick = async () => {
  const btn=document.getElementById("shot"), label=btn.textContent;
  btn.disabled=true; btn.textContent="Capturing…";
  document.body.classList.add("capturing");
  try{
    if(document.fonts&&document.fonts.ready) await document.fonts.ready;
    const ink=getComputedStyle(document.documentElement).getPropertyValue("--ink").trim()||"#0A1440";
    const canvas=await html2canvas(document.querySelector(".wrap"),
      {backgroundColor:ink, scale:2, useCORS:true});
    const a=document.createElement("a");
    a.download=`wind-profile-${new Date().toISOString().slice(0,10)}.png`;
    a.href=canvas.toDataURL("image/png"); a.click();
  }catch(e){ console.error(e); alert("Couldn't capture: "+e.message); }
  finally{ document.body.classList.remove("capturing"); btn.disabled=false; btn.textContent=label; }
};

render();
</script></body></html>
"""


if __name__ == "__main__":
    main()
