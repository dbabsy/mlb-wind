#!/usr/bin/env python3
"""
Daily stadium report: how today's park and weather should tilt each game.

The chain is deliberately empirical — every step is measured from Statcast
rather than assumed:

  1. League baseline. For each (exit velocity, launch angle) bin, how often a
     fly ball becomes a homer, a double, a triple, a single.
  2. Park x wind. Inside each park and wind direction, how far the real
     outcomes sit above or below that baseline, per fly ball.
  3. Temperature. The same residual as a function of game-time temperature,
     league-wide, since warm air carries.
  4. Tonight. Forecast wind is rotated into the park's own frame using the
     bearing fitted in orient.py, the matching cell is looked up, and the
     per-fly-ball edge is scaled by how many fly balls that park sees.
  5. Runs. Extra hits are converted with standard linear weights.

Domed parks are reported as weather-neutral: the roof is the whole point.

    python3 daily.py                 # writes daily.html for today
    python3 daily.py --date 2026-08-25
"""

import argparse
import csv
import json
import math
import ssl
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # no tzdata
    ZoneInfo = None

import wind as W

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

CACHE = Path(__file__).parent / ".cache"
STATS = "https://statsapi.mlb.com/api/v1/schedule"
FORECAST = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "Mozilla/5.0 (wind-profile)"}

# Runs above a generic out. Standard linear weights.
LW = {"hr": 1.40, "x3": 1.03, "x2": 0.75, "x1": 0.47}
# Combined runs in a typical game, used only to express a swing as a percentage.
LEAGUE_RPG = 8.8
HIT = {"home_run": "hr", "triple": "x3", "double": "x2", "single": "x1"}
OUTCOMES = ("hr", "x3", "x2", "x1")

# Where each wind row points, in degrees off the home-plate -> center axis.
ROW_BEARING = {"out_cf": 0, "out_lf": -35, "out_rf": 35,
               "in_cf": 180, "in_lf": 145, "in_rf": 215}
CALM_MPH = 5
# A breeze and a gale from the same quarter are not the same event. League
# median wind is 8 mph and the 90th percentile is 14, so this splits roughly
# three-to-one — fine enough to matter, coarse enough to keep cells populated.
STRONG_MPH = 12
MIN_CELL = 150


def speed_band(mph):
    return "B" if (mph or 0) > STRONG_MPH else "A"


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read())


def load_history():
    winds = {}
    for p in sorted(CACHE.glob("wind_*.json")):
        winds.update(json.loads(p.read_text()))
    rows = []
    for p in sorted(CACHE.glob("sc_*.csv")):
        for r in csv.DictReader(p.open()):
            ev, la = W.fnum(r["launch_speed"]), W.fnum(r["launch_angle"])
            g = winds.get(r["game_pk"])
            if ev is None or la is None or la <= 25 or not g:
                continue
            rows.append((ev, la, r["events"], g, r["game_pk"]))
    return winds, rows


def build_model(rows):
    """League baseline, then park/wind and temperature residuals."""
    acc = defaultdict(lambda: defaultdict(int))
    for ev, la, e, _g, _pk in rows:
        k = (int(ev // 2), int(la // 2))
        acc[k]["n"] += 1
        acc[k][HIT.get(e, "out")] += 1
    base = {k: {o: v[o] / v["n"] for o in OUTCOMES}
            for k, v in acc.items() if v["n"] >= 25}

    park = defaultdict(lambda: defaultdict(float))
    park_n = defaultdict(int)
    temp = defaultdict(lambda: defaultdict(float))
    temp_n = defaultdict(int)
    games_at = defaultdict(set)
    balls_at = defaultdict(int)

    for ev, la, e, g, pk in rows:
        b = base.get((int(ev // 2), int(la // 2)))
        if not b:
            continue
        o = HIT.get(e, "out")
        vid = g.get("venue_id")
        speed, label = W.parse_wind(g.get("wind"))
        row = W.wind_row(speed, label)
        # Accumulate the speed-banded cell and the pooled one together, so a
        # thin band can fall back to every speed from the same quarter.
        keys = [(vid, row, "*")]
        if row != "light":
            keys.append((vid, row, speed_band(speed)))
        for key in keys:
            for k2 in OUTCOMES:
                park[key][k2] += (1.0 if o == k2 else 0.0) - b[k2]
            park_n[key] += 1
        games_at[vid].add(pk)
        balls_at[vid] += 1

        t = W.fnum(g.get("temp"))
        if t is not None:
            tb = int(t // 5) * 5
            for k2 in OUTCOMES:
                temp[tb][k2] += (1.0 if o == k2 else 0.0) - b[k2]
            temp_n[tb] += 1

    park_delta = {f"{v}|{r}|{b}": {k: park[(v, r, b)][k] / park_n[(v, r, b)] for k in OUTCOMES}
                  for (v, r, b) in park_n if park_n[(v, r, b)] >= MIN_CELL}
    park_cnt = {f"{v}|{r}|{b}": park_n[(v, r, b)]
                for (v, r, b) in park_n if park_n[(v, r, b)] >= MIN_CELL}
    # Temperature curve, centred so a league-average night contributes nothing.
    temp_delta = {tb: {k: temp[tb][k] / temp_n[tb] for k in OUTCOMES}
                  for tb in temp_n if temp_n[tb] >= 400}
    mid = {k: sum(temp_delta[t][k] for t in temp_delta) / len(temp_delta) for k in OUTCOMES}
    for t in temp_delta:
        for k in OUTCOMES:
            temp_delta[t][k] -= mid[k]

    fb_per_game = {v: balls_at[v] / len(games_at[v]) for v in games_at if games_at[v]}
    return park_delta, park_cnt, temp_delta, fb_per_game


def dome_parks(winds):
    """Parks whose games are overwhelmingly logged as roofed."""
    tot, domed = defaultdict(int), defaultdict(int)
    for g in winds.values():
        vid = g.get("venue_id")
        tot[vid] += 1
        if "dome" in (g.get("cond") or "").lower() or "roof" in (g.get("cond") or "").lower():
            domed[vid] += 1
    return {v for v in tot if tot[v] >= 50 and domed[v] / tot[v] > 0.6}


def rel_bearing(from_deg, cf_bearing):
    """Forecast wind, expressed as degrees off the park's center-field axis."""
    toward = (from_deg + 180.0) % 360.0
    return (toward - cf_bearing + 180.0) % 360.0 - 180.0


def row_for(rel):
    best, gap = "light", 999
    for row, b in ROW_BEARING.items():
        d = abs((rel - b + 180) % 360 - 180)
        if d < gap:
            best, gap = row, d
    return best if gap <= 45 else "light"


def baseball_today():
    """MLB's day runs on Eastern time. A build at 00:30 UTC is still the
    previous evening's slate in the States, so never key off UTC."""
    tz = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc
    return datetime.now(tz).date()


def forecast_at(lat, lon, when):
    """Hourly forecast for one park at first pitch. {} if unavailable."""
    hour = when.astimezone(timezone.utc)
    try:
        j = get(f"{FORECAST}?" + urllib.parse.urlencode({
            "latitude": round(lat, 4), "longitude": round(lon, 4),
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
            "start_date": hour.date().isoformat(), "end_date": hour.date().isoformat(),
            "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
            "timezone": "UTC"}))
        h = j["hourly"]
        i = h["time"].index(hour.strftime("%Y-%m-%dT%H:00"))
        return {"temp": h["temperature_2m"][i], "mph": h["wind_speed_10m"][i],
                "from": h["wind_direction_10m"][i]}
    except Exception:  # noqa: BLE001
        return {}


def forecast_cell(game, vid, coords, domes, orient):
    """Tonight's weather for a game, and which model cell it selects."""
    lat, lon = coords.get("latitude"), coords.get("longitude")
    start = game.get("gameDate", "")
    if not (lat and start):
        return {}, "light", "A"
    wx = forecast_at(lat, lon, datetime.fromisoformat(start.replace("Z", "+00:00")))
    is_dome = vid in domes
    info = orient.get(str(vid))
    rel = None
    if wx and info and not is_dome:
        rel = rel_bearing(wx["from"], info["bearing"])
    row = ("light" if (is_dome or rel is None or (wx.get("mph") or 0) <= CALM_MPH)
           else row_for(rel))
    return wx, row, speed_band(wx.get("mph"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=baseball_today().isoformat())
    ap.add_argument("--out", default="daily.html")
    a = ap.parse_args()

    winds, rows = load_history()
    print(f"{len(rows):,} historical fly balls", flush=True)
    park_delta, park_cnt, temp_delta, fb_per_game = build_model(rows)
    print(f"{len(park_delta)} park/wind cells, {len(temp_delta)} temperature bins", flush=True)
    domes = dome_parks(winds)
    orient = json.loads((CACHE / "orient.json").read_text()) if (CACHE / "orient.json").exists() else {}

    sched = get(f"{STATS}?" + urllib.parse.urlencode({
        "sportId": 1, "date": a.date, "hydrate": "venue(location),team"}))
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    print(f"{len(games)} games on {a.date}", flush=True)

    out = []
    for g in games:
        ven = g.get("venue") or {}
        vid = ven.get("id")
        crd = ((ven.get("location") or {}).get("defaultCoordinates") or {})
        lat, lon = crd.get("latitude"), crd.get("longitude")
        start = g.get("gameDate", "")
        if not (vid and lat and start):
            continue

        wx, row, band = forecast_cell(g, vid, crd, domes, orient)
        is_dome = vid in domes
        rel = None
        if wx and orient.get(str(vid)) and not is_dome:
            rel = rel_bearing(wx["from"], orient[str(vid)]["bearing"])
        chain = [f"{vid}|{row}|{band}", f"{vid}|{row}|*", f"{vid}|light|*"]
        src = next((c for c in chain if c in park_delta), None)
        d = dict(park_delta.get(src) or {k: 0.0 for k in OUTCOMES})
        if wx and not is_dome:
            tb = int((wx["temp"] or 70) // 5) * 5
            for k in OUTCOMES:
                d[k] += (temp_delta.get(tb) or {}).get(k, 0.0)

        fb = fb_per_game.get(vid, 18.0)
        counts = {k: d[k] * fb for k in OUTCOMES}
        runs = sum(counts[k] * LW[k] for k in OUTCOMES)
        # Share of a typical combined game total, so the swing has a sense of scale.
        pct = runs / LEAGUE_RPG * 100.0
        out.append({
            "venue": ven.get("name", ""),
            "away": g["teams"]["away"]["team"].get("abbreviation", ""),
            "home": g["teams"]["home"]["team"].get("abbreviation", ""),
            "start": start,
            "dome": is_dome,
            "wx": wx,
            "rel": None if rel is None else round(rel, 0),
            "row": row,
            "conf": (orient.get(str(vid)) or {}).get("agree"),
            "hr": round(counts["hr"], 2),
            "xb": round(counts["x2"] + counts["x3"], 2),
            "b1": round(counts["x1"], 2),
            "runs": round(runs, 2),
            "pct": round(pct, 0),
            "n": park_cnt.get(src, 0),
            "cell": src,
            "band": None if (is_dome or row == "light") else band,
        })
    out.sort(key=lambda r: -r["runs"])

    payload = {"games": out, "date": a.date,
               "built": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    Path(a.out).write_text(
        TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"))),
        encoding="utf-8")
    print(f"wrote {a.out}")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Stadium Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --up:#31D07E; --down:#FF4D6A;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto}
header{padding:20px 20px 10px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.eyebrow{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--amber)}
h1{margin:2px 0 0;font-size:26px;font-weight:800;letter-spacing:-.035em}
.meta{font-family:var(--mono);font-size:10px;color:var(--faint);margin-top:5px;letter-spacing:.03em}
.slate{font-size:13px;font-weight:600;color:var(--text);margin-top:4px;letter-spacing:-.01em}
.stamps{display:flex;gap:6px 14px;flex-wrap:wrap;margin-top:4px}
.stamp{font-family:var(--mono);font-size:10px;color:var(--faint);letter-spacing:.03em}
.stamp b{color:var(--dim);font-weight:600}
.stamp.stale{color:var(--amber)}
.stamp.stale b{color:var(--amber)}

.pill{padding:6px 12px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--amber);
  background:var(--panel);border:1px solid var(--amberDim);border-radius:100px;cursor:pointer}
.pill:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
body.capturing .pill{visibility:hidden}
.scroll{padding:0 20px 8px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:620px}
th,td{padding:9px 8px;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
thead th{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--amber);
  text-align:right;border-bottom:1px solid var(--line);font-weight:600}
thead th.l{text-align:left}
tbody tr{border-bottom:1px solid rgba(53,74,148,.3)}
tbody tr:hover{background:rgba(53,74,148,.16)}
td.runs{font-size:19px;font-weight:700;text-align:right;width:78px}
.pctv{display:block;font-size:10px;font-weight:600;color:var(--faint);margin-top:-2px}
td.ven{font-size:14px;font-weight:600;letter-spacing:-.01em}
td.ven small{display:block;font-family:var(--mono);font-size:10px;color:var(--dim);
  font-weight:400;letter-spacing:.05em;margin-top:1px}
td.wx{text-align:center;color:var(--dim);font-size:11px;width:104px}
.arrow{display:inline-block;font-size:15px;color:var(--amber);line-height:1}
td.stat{text-align:right;font-size:13px;font-weight:700;width:64px}
td.stat small{display:block;font-size:9.5px;font-weight:400;color:var(--faint)}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--dim)}
footer{padding:14px 20px 26px;font-family:var(--mono);font-size:10px;color:var(--faint);line-height:1.75}
footer b{color:var(--dim);font-weight:600}
.empty{padding:24px 20px;font-family:var(--mono);font-size:12px;color:var(--faint)}
</style></head><body><div class="wrap">
<header>
  <div>
    <div class="eyebrow">MLB · PARK &amp; WEATHER</div>
    <h1>Daily Stadium Report</h1>
    <div class="slate" id="slate"></div>
    <div class="stamps" id="stamps"></div>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <a class="pill" href="index.html" style="text-decoration:none">Wind profile</a>
    <a class="pill" href="players.html" style="text-decoration:none">Players &rarr;</a>
    <button class="pill" id="shot" type="button">Save image</button>
  </div>
</header>
<div class="scroll" id="scroll"></div>
<footer>
  <b>Runs</b> is the projected swing versus a neutral park on an average night, counting both
  teams. It is built from measured Statcast outcomes: how often fly balls at this park, with the
  wind blowing this way, beat the league rate for the same exit velocity and launch angle &mdash;
  scaled by how many fly balls the park sees and converted with linear weights.<br>
  Wind is tonight's forecast at first pitch, rotated into the park's own frame. Roofed parks are
  shown weather-neutral. <b>Projections, not predictions</b> &mdash; this is park and air only,
  and says nothing about who is pitching.
</footer></div>
<script>
const D = __DATA__;
const ROW_LABEL = {out_cf:"out to CF",out_lf:"out to LF",out_rf:"out to RF",
  in_cf:"in from CF",in_lf:"in from LF",in_rf:"in from RF",light:"light / cross"};

function cls(v,eps){ return v>eps?"up":(v<-eps?"down":"flat"); }
function sgn(v,dp){ return (v>0?"+":"")+v.toFixed(dp===undefined?2:dp); }

function row(g){
  const pct = g.base ? "" : "";
  const arrow = g.dome ? `<span style="font-size:13px">&#127968; roof</span>`
    : (g.wx && g.wx.mph!=null
      ? `<span class="arrow" style="transform:rotate(${(g.rel||0)}deg)">&#8593;</span>
         ${Math.round(g.wx.mph)}mph<br><small>${ROW_LABEL[g.row]} · ${Math.round(g.wx.temp)}&deg;</small>`
      : `<small>no forecast</small>`);
  return `<tr>
    <td class="runs ${cls(g.runs,.15)}">${sgn(g.runs)}
      <span class="pctv">${sgn(g.pct,0)}%</span></td>
    <td class="ven">${g.venue}<small>${g.away} @ ${g.home}</small></td>
    <td class="wx">${arrow}</td>
    <td class="stat ${cls(g.hr,.05)}">${sgn(g.hr)}<small>HR</small></td>
    <td class="stat ${cls(g.xb,.05)}">${sgn(g.xb)}<small>2B/3B</small></td>
    <td class="stat ${cls(g.b1,.05)}">${sgn(g.b1)}<small>1B</small></td>
  </tr>`;
}


// ---- header stamp: what day is shown, and how fresh the pull is ----
function etTime(iso){
  return new Date(iso).toLocaleString("en-US",{timeZone:"America/New_York",
    hour:"numeric",minute:"2-digit",hour12:true}) + " ET";
}
function etDateShort(iso){
  return new Date(iso).toLocaleDateString("en-US",{timeZone:"America/New_York",
    month:"short",day:"numeric"});
}
function ago(iso){
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(s<0) return "just now";
  if(s<90) return "just now";
  if(s<5400) return Math.round(s/60)+" min ago";
  if(s<172800) return Math.round(s/3600)+" hr ago";
  return Math.round(s/86400)+" days ago";
}
function dayLabel(ymd){
  const [y,m,d]=ymd.split("-").map(Number);
  return new Date(Date.UTC(y,m-1,d)).toLocaleDateString("en-US",
    {timeZone:"UTC",weekday:"long",month:"long",day:"numeric"});
}
// Anything older than this probably means a build failed rather than a quiet day.
const STALE_HRS = 4;
function stampHTML(iso, extra){
  const hrs=(Date.now()-new Date(iso).getTime())/3.6e6;
  const stale = hrs > STALE_HRS;
  return `<span class="stamp${stale?" stale":""}" title="${new Date(iso).toString()}">
    <b>Updated</b> ${etTime(iso)} on ${etDateShort(iso)} · ${ago(iso)}${stale?" — may be out of date":""}
  </span>${extra?`<span class="stamp">${extra}</span>`:""}`;
}
function startStampTicker(fn){ fn(); setInterval(fn, 60000); }

document.getElementById("slate").textContent =
  `${dayLabel(D.date)} · ${D.games.length} game${D.games.length===1?"":"s"}`;
startStampTicker(()=>{ document.getElementById("stamps").innerHTML =
  stampHTML(D.built, "forecast at first pitch"); });

document.getElementById("scroll").innerHTML = D.games.length ? `<table>
  <thead><tr><th>Runs</th><th class="l">Venue</th><th style="text-align:center">Wind</th>
  <th>HR</th><th>2B/3B</th><th>1B</th></tr></thead>
  <tbody>${D.games.map(row).join("")}</tbody></table>`
  : `<div class="empty">No games scheduled.</div>`;

document.getElementById("shot").onclick = async () => {
  const btn=document.getElementById("shot"), label=btn.textContent;
  btn.disabled=true; btn.textContent="Capturing…";
  document.body.classList.add("capturing");
  try{
    if(document.fonts&&document.fonts.ready) await document.fonts.ready;
    const ink=getComputedStyle(document.documentElement).getPropertyValue("--ink").trim()||"#0A1440";
    const c=await html2canvas(document.querySelector(".wrap"),{backgroundColor:ink,scale:2,useCORS:true});
    const a=document.createElement("a");
    a.download=`daily-stadium-report-${D.date}.png`; a.href=c.toDataURL("image/png"); a.click();
  }catch(e){ console.error(e); alert("Couldn't capture: "+e.message); }
  finally{ document.body.classList.remove("capturing"); btn.disabled=false; btn.textContent=label; }
};
</script></body></html>
"""


if __name__ == "__main__":
    main()
