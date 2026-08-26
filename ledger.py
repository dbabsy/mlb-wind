#!/usr/bin/env python3
"""
Accuracy ledger — score the projections against what actually happened.

Every other page in this repo asserts that its model works on the strength of
a backtest run once, by hand, on a window I chose. This keeps score instead.

Three rules make the record honest:

  1. A prediction is only recorded **before first pitch**. Nothing is written
     for a game already under way, so a result can never leak backwards into
     the number that predicted it.
  2. A recorded prediction is never revised. Re-running the build re-writes a
     game's row only while that game is still in the future; once it starts,
     the row is frozen exactly as it stood.
  3. Scoring is a separate pass that only touches finished games, and only
     fills in a result that was previously blank.

The ledger lives in the repository rather than a cache, so the history
survives, is versioned, and can be audited commit by commit.

    python3 ledger.py --record     # freeze today's pre-game predictions
    python3 ledger.py --score      # fill in results for finished games
    python3 ledger.py --render     # build accuracy.html
    python3 ledger.py --all        # all three, which is what CI runs
"""

import argparse
import json
import re
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import players as P

HERE = Path(__file__).parent
LEDGER = HERE / "ledger" / "predictions.json"
API = "https://statsapi.mlb.com/api/v1"


def load():
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except ValueError:
            pass
    return {"hits": [], "games": []}


def save(led):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    for k in led:
        led[k].sort(key=lambda r: (r["date"], r.get("gamePk", 0), r.get("pid", 0)))
    LEDGER.write_text(json.dumps(led, separators=(",", ":"), indent=0))


def embedded(path):
    """Pull the payload back out of a page this repo just built."""
    if not Path(path).exists():
        return None
    m = re.search(r"const D = (\{.*?\});\n", Path(path).read_text(), re.S)
    return json.loads(m.group(1)) if m else None


def not_started(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


def record(led, hits_html, games_html):
    """Freeze predictions for games that have not begun."""
    added = updated = skipped = 0

    H = embedded(hits_html)
    if H:
        idx = {(r["date"], r["gamePk"], r["pid"]): r for r in led["hits"]}
        for g in H["games"]:
            gp = g.get("gamePk")
            if not gp:
                continue
            if not not_started(g.get("start", "")):
                skipped += 1
                continue
            for c in g["picks"]:
                key = (H["date"], gp, c["id"])
                row = idx.get(key)
                if row and row.get("result") is not None:
                    continue            # already settled; never touch it
                payload = {"date": H["date"], "gamePk": gp, "pid": c["id"],
                           "name": c["name"], "team": c["team"], "slot": c["slot"],
                           "p": c["p"], "result": None}
                if row:
                    row.update(payload)
                    updated += 1
                else:
                    led["hits"].append(payload)
                    idx[key] = payload
                    added += 1

    G = embedded(games_html)
    if G:
        idx = {(r["date"], r["gamePk"]): r for r in led["games"]}
        for g in G["games"]:
            gp = g.get("gamePk")
            if not gp or not not_started(g.get("start", "")):
                continue
            key = (G["date"], gp)
            row = idx.get(key)
            if row and row.get("result") is not None:
                continue
            payload = {"date": G["date"], "gamePk": gp,
                       "home": g["home"]["team"], "away": g["away"]["team"],
                       "wpHome": g["wpHome"], "rHome": g["home"]["runs"],
                       "rAway": g["away"]["runs"], "result": None}
            if row:
                row.update(payload)
                updated += 1
            else:
                led["games"].append(payload)
                idx[key] = payload
                added += 1

    print(f"  recorded: {added} new, {updated} refreshed, {skipped} games already under way")
    return led


def score(led):
    """Fill in outcomes for finished games only."""
    open_hit_dates = sorted({r["date"] for r in led["hits"] if r["result"] is None})
    open_game_dates = sorted({r["date"] for r in led["games"] if r["result"] is None})
    filled = 0

    for d in open_hit_dates:
        try:
            j = P.q("stats", stats="byDateRange", group="hitting", season=d[:4],
                    sportId=1, gameType="R", startDate=d, endDate=d,
                    limit=2000, playerPool="All")
            got = {}
            for s in j["stats"][0]["splits"]:
                st = s.get("stat") or {}
                if int(st.get("plateAppearances") or 0) >= 1:
                    got[s["player"]["id"]] = int(st.get("hits") or 0) >= 1
        except Exception as e:  # noqa: BLE001
            print(f"  hits {d}: fetch failed ({type(e).__name__})")
            continue
        if not got:
            continue
        for r in led["hits"]:
            if r["date"] == d and r["result"] is None and r["pid"] in got:
                r["result"] = got[r["pid"]]
                filled += 1

    for d in open_game_dates:
        try:
            j = P.q("schedule", sportId=1, gameType="R", date=d, hydrate="team")
            res = {}
            for dd in j.get("dates", []):
                for g in dd.get("games", []):
                    if (g.get("status") or {}).get("codedGameState") != "F":
                        continue
                    h, a = g["teams"]["home"], g["teams"]["away"]
                    if "score" in h and "score" in a:
                        res[g["gamePk"]] = h["score"] > a["score"]
        except Exception as e:  # noqa: BLE001
            print(f"  games {d}: fetch failed ({type(e).__name__})")
            continue
        for r in led["games"]:
            if r["date"] == d and r["result"] is None and r["gamePk"] in res:
                r["result"] = res[r["gamePk"]]
                filled += 1

    print(f"  scored: {filled} outcomes filled in")
    return led


def brier(rows, key="p"):
    n = len(rows)
    return sum((r[key] - (1 if r["result"] else 0)) ** 2 for r in rows) / n if n else None


def summarise(led):
    hits = [r for r in led["hits"] if r["result"] is not None]
    games = [r for r in led["games"] if r["result"] is not None]

    def buckets(rows, key, width=0.05):
        b = defaultdict(list)
        for r in rows:
            b[round(r[key] / width) * width].append(1 if r["result"] else 0)
        return [{"p": k, "n": len(v), "actual": sum(v) / len(v)}
                for k, v in sorted(b.items()) if len(v) >= 10]

    def daily(rows, key):
        by = defaultdict(list)
        for r in rows:
            by[r["date"]].append(r)
        return [{"date": d,
                 "n": len(v),
                 "pred": sum(x[key] for x in v) / len(v),
                 "actual": sum(1 for x in v if x["result"]) / len(v)}
                for d, v in sorted(by.items())]

    out = {"built": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    if hits:
        out["hits"] = {
            "n": len(hits),
            "pred": sum(r["p"] for r in hits) / len(hits),
            "actual": sum(1 for r in hits if r["result"]) / len(hits),
            "brier": brier(hits),
            "buckets": buckets(hits, "p"),
            "daily": daily(hits, "p"),
            "pending": sum(1 for r in led["hits"] if r["result"] is None),
        }
    if games:
        fav = [r for r in games
               if (r["wpHome"] >= 0.5) == bool(r["result"])]
        out["games"] = {
            "n": len(games),
            "pred": sum(r["wpHome"] for r in games) / len(games),
            "actual": sum(1 for r in games if r["result"]) / len(games),
            "brier": brier(games, "wpHome"),
            "favAcc": len(fav) / len(games),
            "buckets": buckets(games, "wpHome"),
            "daily": daily(games, "wpHome"),
            "pending": sum(1 for r in led["games"] if r["result"] is None),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hits-html", default="public/hits.html")
    ap.add_argument("--games-html", default="public/games.html")
    ap.add_argument("--out", default="accuracy.html")
    a = ap.parse_args()
    if a.all:
        a.record = a.score = a.render = True

    led = load()
    if a.record:
        led = record(led, a.hits_html, a.games_html)
    if a.score:
        led = score(led)
    if a.record or a.score:
        save(led)
        print(f"  ledger: {len(led['hits'])} hit picks, {len(led['games'])} games")
    if a.render:
        payload = summarise(led)
        Path(a.out).write_text(
            TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"))),
            encoding="utf-8")
        h = payload.get("hits")
        if h:
            print(f"  hits: {h['n']} scored, predicted {h['pred']:.3f} "
                  f"actual {h['actual']:.3f}")
        g = payload.get("games")
        if g:
            print(f"  games: {g['n']} scored, favourite {g['favAcc']:.1%}")
        print(f"wrote {a.out}")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Accuracy Ledger</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --good:#31D07E; --bad:#FF4D6A;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto}
header{padding:20px 20px 8px}
.eyebrow{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--amber)}
h1{margin:2px 0 0;font-size:26px;font-weight:800;letter-spacing:-.035em}
.stamp{font-family:var(--mono);font-size:10px;color:var(--faint);margin-top:5px}
.nav{padding:8px 20px 12px;display:flex;gap:8px;flex-wrap:wrap}
.pill{padding:6px 12px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--amber);
  background:var(--panel);border:1px solid var(--amberDim);border-radius:100px;
  text-decoration:none;display:inline-block}
.pill:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
.card{margin:0 20px 14px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.ch{padding:11px 14px;border-bottom:1px solid var(--line);display:flex;
  justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.ch b{font-size:15px;font-weight:700}
.ch span{font-family:var(--mono);font-size:10px;color:var(--faint)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;
  background:var(--line)}
.kpi{background:var(--panel);padding:11px 14px}
.kpi b{display:block;font-family:var(--mono);font-size:21px;font-weight:800;letter-spacing:-.02em}
.kpi span{font-family:var(--mono);font-size:9px;color:var(--faint);letter-spacing:.07em;text-transform:uppercase}
.kpi small{font-family:var(--mono);font-size:9.5px;color:var(--dim);display:block;margin-top:2px}
table{border-collapse:collapse;width:100%}
th,td{padding:6px 14px;font-family:var(--mono);font-size:11px;text-align:right;
  font-variant-numeric:tabular-nums}
th{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--amber);
  border-bottom:1px solid var(--line);font-weight:600}
th.l,td.l{text-align:left}
tbody tr+tr td{border-top:1px solid rgba(53,74,148,.22)}
.rel{display:flex;align-items:center;gap:6px;justify-content:flex-end}
.relbar{width:74px;height:5px;background:var(--panel2);border-radius:100px;position:relative}
.relbar i{position:absolute;top:0;height:100%;border-radius:100px;background:var(--good)}
.relbar u{position:absolute;top:-3px;width:2px;height:11px;background:var(--amber);text-decoration:none}
.empty{padding:22px 14px;font-family:var(--mono);font-size:12px;color:var(--faint);line-height:1.7}
footer{padding:14px 20px 26px;font-family:var(--mono);font-size:10px;color:var(--faint);line-height:1.75}
footer b{color:var(--dim)}
</style></head><body><div class="wrap">
<header>
  <div class="eyebrow">MLB · KEEPING SCORE</div>
  <h1>Accuracy Ledger</h1>
  <div class="stamp" id="stamp"></div>
</header>
<div class="nav">
  <a class="pill" href="hits.html">Hit picks</a>
  <a class="pill" href="games.html">Matchups</a>
  <a class="pill" href="players.html">Players</a>
  <a class="pill" href="daily.html">Stadium report</a>
</div>
<div id="board"></div>
<footer>
  Predictions are frozen <b>before first pitch</b> and never revised afterwards, so nothing a
  model got told later can leak into what it said earlier. Results are filled in once a game
  is final. The ledger is committed to the repository, so every entry is auditable.<br>
  <b>Calibration</b> is the gap between what was predicted and what happened — near zero is the
  goal, and it matters more than the raw hit rate. <b>Brier</b> scores probability quality:
  lower is better, and beating the always-guess-the-base-rate line is the bar.
</footer></div>
<script>
const D = __DATA__;
function pc(v,d){ return (v*100).toFixed(d===undefined?1:d)+"%"; }
function relRow(b, ideal){
  const off = b.actual - b.p;
  return `<tr><td class="l">${pc(b.p,0)}</td><td>${b.n}</td>
    <td>${pc(b.actual,0)}</td>
    <td><span class="rel"><span class="relbar">
      <i style="left:0;width:${Math.min(100,b.actual*100)}%"></i>
      <u style="left:${Math.min(100,b.p*100)}%"></u></span>
      <span style="color:${Math.abs(off)<0.05?'var(--dim)':(off>0?'var(--good)':'var(--bad)')}">
      ${off>0?"+":""}${(off*100).toFixed(0)}</span></span></td></tr>`;
}
function section(key, title, note, predLabel){
  const s = D[key];
  if(!s) return `<div class="card"><div class="ch"><b>${title}</b></div>
    <div class="empty">No scored predictions yet. The ledger fills in the morning after
    the first slate it records — check back tomorrow.</div></div>`;
  const gap = s.pred - s.actual;
  const recent = s.daily.slice(-10).reverse();
  return `<div class="card">
    <div class="ch"><b>${title}</b><span>${note}${s.pending?` · ${s.pending} awaiting results`:""}</span></div>
    <div class="kpis">
      <div class="kpi"><span>scored</span><b>${s.n.toLocaleString()}</b><small>${s.daily.length} days</small></div>
      <div class="kpi"><span>predicted</span><b>${pc(s.pred)}</b><small>${predLabel}</small></div>
      <div class="kpi"><span>actual</span><b>${pc(s.actual)}</b><small>what happened</small></div>
      <div class="kpi"><span>calibration</span>
        <b style="color:${Math.abs(gap)<0.02?'var(--good)':Math.abs(gap)<0.05?'var(--amber)':'var(--bad)'}">
        ${gap>0?"+":""}${(gap*100).toFixed(1)}</b><small>points off</small></div>
      <div class="kpi"><span>brier</span><b>${s.brier.toFixed(3)}</b><small>lower is better</small></div>
      ${s.favAcc!==undefined?`<div class="kpi"><span>favourite won</span><b>${pc(s.favAcc,0)}</b><small>pick accuracy</small></div>`:""}
    </div>
    ${s.buckets.length?`<table><thead><tr><th class="l">Predicted</th><th>N</th><th>Actual</th>
      <th>Reliability</th></tr></thead><tbody>${s.buckets.map(relRow).join("")}</tbody></table>`:""}
    ${recent.length?`<table><thead><tr><th class="l">Recent days</th><th>N</th><th>Pred</th>
      <th>Actual</th></tr></thead><tbody>${recent.map(d=>`<tr><td class="l">${d.date}</td>
      <td>${d.n}</td><td>${pc(d.pred,0)}</td>
      <td style="color:${d.actual>=d.pred?'var(--good)':'var(--bad)'}">${pc(d.actual,0)}</td></tr>`).join("")}
      </tbody></table>`:""}
  </div>`;
}
document.getElementById("stamp").textContent =
  "updated " + new Date(D.built).toLocaleString("en-US",{timeZone:"America/Chicago",
    month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) + " CT";
document.getElementById("board").innerHTML =
  section("hits","Hit picks","did the picked hitter record a hit","model's average")
  + section("games","Matchup win probabilities","did the home team win","average home win prob");
</script></body></html>
"""


if __name__ == "__main__":
    main()
