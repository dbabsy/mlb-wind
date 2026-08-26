#!/usr/bin/env python3
"""
Best hit bets — the likeliest players to record a hit in each game.

The players page already carries a 1+ hit number, but it gets there by taking
a hitter's hits-per-plate-appearance and scaling it. That throws away the
structure of the event. This does it properly:

    a plate appearance is a walk, a hit by pitch, a strikeout, a home run,
    or a ball in play — and only the last two can become a hit

so each component is modelled on its own terms and recombined:

    P(hit) = P(HR) + P(ball in play) x BABIP

Two things matter more than the decomposition itself.

First, batter and pitcher are combined with the **odds ratio** (log5), not by
multiplying rates. A contact hitter against a strikeout pitcher should land
between the two, weighted by how extreme each is; multiplying overshoots and
can push rates past 1.

Second, every component is regressed by **how quickly that statistic actually
stabilises**. Strikeout rate settles in about 60 plate appearances and BABIP
takes many hundreds of balls in play, so treating them with one blanket
regression — as a single hits-per-PA number does — trusts a hitter's BABIP
far too early. That is the main reason this disagrees with the players page.

Facing pitching is 59% the starter and 41% the bullpen, the league's real
split. Park and weather move home runs and balls in play separately.

Sprint speed enters as the BABIP *prior*, never as a bonus: a hitter's own
BABIP already contains his legs, so adding speed on top would count them
twice. What speed changes is what a thin sample regresses toward — measured
at about +0.006 BABIP per foot per second this season.

    python3 hits.py                  # today
    python3 hits.py --date 2026-08-25 --picks 3
"""

import argparse
import csv
import io
import json
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import daily as Dl
import players as P

CACHE = Path(__file__).parent / ".cache"

HIT_KEYS = P.HIT_KEYS + ("sacFlies",)
SP_SHARE = 0.59

# Plate appearances each rate needs before it means much. These are the
# published stabilisation points, and they differ by an order of magnitude —
# which is exactly why one blanket regression is the wrong tool.
REG = {"k": 60, "bb": 120, "hbp": 200, "hr": 170, "babip": 400}

# Balls in play per plate appearance, and the share of those that are fly
# balls — used to scale the wind model's per-fly-ball effect onto BABIP.
BIP_PER_PA = 0.68
FLY_PER_BIP = 0.21


SAVANT_SPEED = ("https://baseballsavant.mlb.com/leaderboard/sprint_speed"
                "?year={year}&position=&team=&min=10&csv=true")


def sprint_speed(season):
    """Statcast sprint speed by player id, cached for the day."""
    path = CACHE / f"speed_{season}.json"
    if path.exists():
        try:
            return {int(k): v for k, v in json.loads(path.read_text()).items()}
        except ValueError:
            pass
    out = {}
    try:
        req = urllib.request.Request(SAVANT_SPEED.format(year=season),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90, context=P.SSL_CTX) as r:
            raw = r.read().decode("utf-8-sig")
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                out[int(row["player_id"])] = float(row["sprint_speed"])
            except (ValueError, KeyError):
                continue
        path.write_text(json.dumps(out))
    except Exception as e:  # noqa: BLE001 - speed is an refinement, not a requirement
        print(f"  sprint speed unavailable ({type(e).__name__}); using flat BABIP prior",
              flush=True)
    return out


def fit_speed_prior(h_season, speed, lg_babip):
    """How much BABIP a foot per second is worth, fitted on this season.

    Speed is not added as a bonus — the hitter's own BABIP already contains
    his legs. It is used to set what his BABIP regresses *toward*, so a fast
    player with few balls in play is no longer dragged to a league mean that
    was never his to begin with."""
    xs, ys = [], []
    for pid, row in h_season.items():
        bip = row["atBats"] - row["strikeOuts"] - row["homeRuns"] + row["sacFlies"]
        spd = speed.get(pid)
        if bip < 150 or spd is None:
            continue
        xs.append(spd)
        ys.append((row["hits"] - row["homeRuns"]) / bip)
    if len(xs) < 60:
        return 0.0, 0.0
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return 0.0, mx
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    # Keep the adjustment bounded; this is a nudge, not a headline effect.
    return max(0.0, min(0.012, slope)), mx


def rate_of(row, key, denom):
    d = row.get(denom, 0.0)
    return (row.get(key, 0.0) / d) if d > 0 else None


def regressed(season, recent, key, denom, league, reg):
    """Season rate pulled toward recent form, then toward league by `reg`."""
    n = season.get(denom, 0.0)
    if n <= 0:
        return league
    p = season.get(key, 0.0) / n
    rn = (recent or {}).get(denom, 0.0)
    if rn > 0:
        w = rn / (rn + P.FORM_K)
        p = (1 - w) * p + w * (recent[key] / rn)
    return (n * p + reg * league) / (n + reg)


def odds_ratio(bat, pit, lg):
    """log5: combine a batter and pitcher rate against the league baseline.

    Multiplying two rates double-counts the league average and can exceed 1;
    working in odds space keeps the result bounded and symmetric."""
    for v in (bat, pit, lg):
        if v is None or v <= 0 or v >= 1:
            return bat if bat is not None else lg
    ob, op, ol = bat / (1 - bat), pit / (1 - pit), lg / (1 - lg)
    o = ob * op / ol
    return o / (1 + o)


def batter_babip(row):
    bip = row.get("atBats", 0) - row.get("strikeOuts", 0) - row.get("homeRuns", 0) \
        + row.get("sacFlies", 0)
    if bip <= 0:
        return None, 0
    return (row.get("hits", 0) - row.get("homeRuns", 0)) / bip, bip


def pitcher_rates(row, lg):
    """K, BB, HR per batter faced and BABIP allowed, for a staff or starter."""
    bf = row.get("bf") or row.get("battersFaced") or 0
    if bf < 150:
        return None
    k = row.get("so", row.get("strikeOuts", 0)) / bf
    bb = row.get("bb", row.get("baseOnBalls", 0)) / bf
    hr = row.get("hr", row.get("homeRuns", 0)) / bf
    h = row.get("h", row.get("hits", 0))
    bip = bf * (1 - k - bb - hr) - bf * 0.01      # rough HBP allowance
    babip = (h - row.get("hr", row.get("homeRuns", 0))) / bip if bip > 0 else None
    return {"k": k, "bb": bb, "hr": hr, "babip": babip, "bf": bf}


def blend_staff(sp, rp):
    """One pitching profile for the whole game, weighted by who faces whom."""
    if sp and rp:
        return {k: SP_SHARE * sp[k] + (1 - SP_SHARE) * rp[k]
                for k in ("k", "bb", "hr", "babip")
                if sp.get(k) is not None and rp.get(k) is not None}
    return sp or rp or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=P.baseball_today().isoformat())
    ap.add_argument("--picks", type=int, default=3, help="picks shown per game")
    ap.add_argument("--out", default="hits.html")
    a = ap.parse_args()
    day = date.fromisoformat(a.date)
    season = day.year
    since = (day - timedelta(days=P.RECENT_DAYS)).isoformat()

    common = dict(season=season, sportId=1, gameType="R", limit=2000, playerPool="All")
    print("pulling league stats …", flush=True)
    h_season = P.stat_map(P.q("stats", stats="season", group="hitting", **common)
                          ["stats"][0]["splits"], HIT_KEYS)
    h_recent = P.stat_map(P.q("stats", stats="byDateRange", group="hitting",
                              startDate=since, endDate=a.date, **common)
                          ["stats"][0]["splits"], HIT_KEYS)
    p_season = P.stat_map(P.q("stats", stats="season", group="pitching", **common)
                          ["stats"][0]["splits"], P.PIT_KEYS)
    splits = {}
    for code, hand in (("vl", "L"), ("vr", "R")):
        splits[hand] = P.stat_map(P.q("stats", stats="statSplits", sitCodes=code,
                                      group="hitting", **common)["stats"][0]["splits"],
                                  HIT_KEYS)

    # Bullpen profile per club.
    bullpen = {}
    j = P.q("teams/stats", stats="statSplits", sitCodes="rp", group="pitching",
            season=season, sportId=1, gameType="R")
    for s in j["stats"][0]["splits"]:
        tid = (s.get("team") or {}).get("id")
        st = s.get("stat") or {}
        if not tid:
            continue
        bullpen[tid] = pitcher_rates(
            {"bf": P.num(st.get("battersFaced")), "so": P.num(st.get("strikeOuts")),
             "bb": P.num(st.get("baseOnBalls")), "hr": P.num(st.get("homeRuns")),
             "h": P.num(st.get("hits"))}, None)

    # League baselines, including a properly computed BABIP.
    tot = defaultdict(float)
    for r in h_season.values():
        for k in HIT_KEYS:
            tot[k] += r[k]
    pa = tot["plateAppearances"]
    lg = {"k": tot["strikeOuts"] / pa, "bb": tot["baseOnBalls"] / pa,
          "hbp": tot["hitByPitch"] / pa, "hr": tot["homeRuns"] / pa}
    lg_bip = tot["atBats"] - tot["strikeOuts"] - tot["homeRuns"] + tot["sacFlies"]
    lg["babip"] = (tot["hits"] - tot["homeRuns"]) / lg_bip
    lg_split = {}
    for hand, tbl in splits.items():
        s = defaultdict(float)
        for r in tbl.values():
            for k in HIT_KEYS:
                s[k] += r[k]
        spa = s["plateAppearances"] or 1
        sbip = s["atBats"] - s["strikeOuts"] - s["homeRuns"] + s["sacFlies"]
        lg_split[hand] = {
            "k": (s["strikeOuts"] / spa) / lg["k"],
            "bb": (s["baseOnBalls"] / spa) / lg["bb"],
            "hr": (s["homeRuns"] / spa) / lg["hr"],
            "babip": ((s["hits"] - s["homeRuns"]) / sbip) / lg["babip"] if sbip else 1.0,
        }
    print(f"  league: K {lg['k']:.3f}  BB {lg['bb']:.3f}  HR {lg['hr']:.4f}  "
          f"BABIP {lg['babip']:.3f}", flush=True)

    speed = sprint_speed(season)
    spd_slope, spd_mean = fit_speed_prior(h_season, speed, lg["babip"])
    print(f"  sprint speed: {len(speed)} players · {spd_slope:+.5f} BABIP per ft/s "
          f"(mean {spd_mean:.2f})", flush=True)

    park_delta, _pc, _td, _fb, domes = Dl.load_model()
    orient = json.loads((CACHE / "orient.json").read_text()) \
        if (CACHE / "orient.json").exists() else {}

    sched = P.q("schedule", sportId=1, date=a.date,
                hydrate="probablePitcher,lineups,team,venue(location)")
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    fallback = P.recent_lineups(day)
    print(f"{len(games)} games on {a.date}", flush=True)

    hands, names = {}, {}
    for g in games:
        for side in ("home", "away"):
            tid = g["teams"][side]["team"]["id"]
            if tid in hands:
                continue
            hands[tid] = True
            try:
                for e in P.q(f"teams/{tid}/roster", rosterType="active",
                             hydrate="person")["roster"]:
                    pr = e["person"]
                    names[pr["id"]] = {
                        "name": pr.get("fullName", ""),
                        "bat": (pr.get("batSide") or {}).get("code", "R"),
                        "throw": (pr.get("pitchHand") or {}).get("code", "R"),
                    }
            except Exception:  # noqa: BLE001
                pass

    out = []
    for g in games:
        ven = g.get("venue") or {}
        vid = ven.get("id")
        crd = ((ven.get("location") or {}).get("defaultCoordinates") or {})
        wx, row, band = Dl.forecast_cell(g, vid, crd, domes, orient)
        chain = [f"{vid}|{row}|{band}", f"{vid}|{row}|*", f"{vid}|light|*"]
        src = next((c for c in chain if c in park_delta), None)
        pdl = park_delta.get(src) or {k: 0.0 for k in Dl.OUTCOMES}
        hr_mult = 1 + (pdl["hr"] * P.FLY_PER_PA) / lg["hr"] if lg["hr"] else 1.0
        # Wind moves fly balls; fly balls are only a fifth of balls in play,
        # so the BABIP effect is correspondingly diluted.
        babip_mult = 1 + ((pdl["x1"] + pdl["x2"] + pdl["x3"]) * FLY_PER_BIP) / lg["babip"] \
            if lg["babip"] else 1.0

        cands = []
        for side, other, key in (("away", "home", "awayPlayers"),
                                 ("home", "away", "homePlayers")):
            team = g["teams"][side]["team"]
            opp = g["teams"][other]["team"]
            osp = (g["teams"][other].get("probablePitcher") or {})
            sp_hand = (names.get(osp.get("id"), {}) or {}).get("throw", "R")
            sp_prof = pitcher_rates(p_season.get(osp.get("id")) or {}, lg)
            staff = blend_staff(sp_prof, bullpen.get(opp["id"]))

            ids = [p["id"] for p in ((g.get("lineups") or {}).get(key) or [])]
            confirmed = bool(ids)
            if not ids:
                ids = fallback.get(team["id"], [])[:9]

            for slot, pid in enumerate(ids[:9]):
                s = h_season.get(pid)
                if not s or s.get("plateAppearances", 0) < 40:
                    continue
                r = h_recent.get(pid)
                srow = splits.get(sp_hand, {}).get(pid)
                sh = lg_split[sp_hand]

                def comp(k, statkey, denom="plateAppearances"):
                    base = regressed(s, r, statkey, denom, lg[k], REG[k])
                    pm = P.platoon_mult(srow, s, sh.get(k, 1.0), statkey, denom)
                    return base * pm

                k_b = comp("k", "strikeOuts")
                bb_b = comp("bb", "baseOnBalls")
                hbp_b = comp("hbp", "hitByPitch")
                hr_b = comp("hr", "homeRuns")
                bb_raw, bip_n = batter_babip(s)
                # Regress toward what a hitter with THIS player's legs runs,
                # not toward the league's average pair of legs.
                spd = speed.get(pid)
                prior = lg["babip"] + (spd_slope * (spd - spd_mean) if spd else 0.0)
                babip_b = ((bip_n * bb_raw + REG["babip"] * prior)
                           / (bip_n + REG["babip"])) if bb_raw is not None else prior
                babip_b *= sh.get("babip", 1.0)

                # Combine with the pitching he'll actually face.
                k = odds_ratio(k_b, staff.get("k"), lg["k"])
                bb = odds_ratio(bb_b, staff.get("bb"), lg["bb"])
                hr = odds_ratio(hr_b, staff.get("hr"), lg["hr"]) * hr_mult
                babip = odds_ratio(babip_b, staff.get("babip"), lg["babip"]) * babip_mult

                bip = max(0.0, 1 - k - bb - hbp_b - hr)
                p_hit = min(0.95, hr + bip * babip)
                pa_exp = P.SLOT_PA[slot]
                cands.append({
                    "id": pid, "team": team.get("abbreviation", ""),
                    "name": (names.get(pid, {}) or {}).get("name") or str(pid),
                    "bat": (names.get(pid, {}) or {}).get("bat", "R"),
                    "slot": slot + 1, "pa": round(pa_exp, 1),
                    "p": round(P.p_at_least_one(p_hit, pa_exp), 4),
                    "perPA": round(p_hit, 4),
                    "k": round(k, 3), "babip": round(babip, 3),
                    "spd": round(spd, 1) if spd else None,
                    "vs": osp.get("fullName", "TBD"), "hand": sp_hand,
                    "confirmed": confirmed,
                })

        cands.sort(key=lambda c: -c["p"])
        out.append({
            "venue": ven.get("name", ""), "start": g.get("gameDate", ""),
            "away": g["teams"]["away"]["team"].get("abbreviation", ""),
            "home": g["teams"]["home"]["team"].get("abbreviation", ""),
            "dome": vid in domes, "wx": wx, "row": row,
            "hrMult": round(hr_mult, 3), "babipMult": round(babip_mult, 3),
            "picks": cands[:a.picks], "n": len(cands),
        })

    out.sort(key=lambda g: -(g["picks"][0]["p"] if g["picks"] else 0))
    payload = {"games": out, "date": a.date, "picks": a.picks,
               "built": datetime.now(timezone.utc).isoformat(timespec="minutes"),
               "lgHit": round(lg["babip"], 3)}
    Path(a.out).write_text(
        TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"))),
        encoding="utf-8")
    allp = [c["p"] for g in out for c in g["picks"]]
    if allp:
        print(f"wrote {a.out} — {len(out)} games, {len(allp)} picks, "
              f"mean pick P(hit) {sum(allp)/len(allp):.3f}")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Best Hit Bets</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --hot:#31D07E;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:940px;margin:0 auto}
header{padding:20px 20px 8px}
.eyebrow{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--amber)}
h1{margin:2px 0 0;font-size:26px;font-weight:800;letter-spacing:-.035em}
.slate{margin-top:6px;font-size:14px;font-weight:600}
.stamp{font-family:var(--mono);font-size:10px;color:var(--faint);margin-right:12px}
.stamp b{color:var(--dim);font-weight:600}
.stamp.stale{color:var(--amber)}
.nav{padding:8px 20px 12px;display:flex;gap:8px;flex-wrap:wrap}
.pill{padding:6px 12px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--amber);
  background:var(--panel);border:1px solid var(--amberDim);border-radius:100px;
  text-decoration:none;display:inline-block;cursor:pointer}
.pill:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
.pill.on{background:var(--amber);color:var(--ink)}
.game{margin:0 20px 8px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.gh{padding:9px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;
  align-items:baseline;flex-wrap:wrap}
.gh b{font-size:14px;font-weight:700;letter-spacing:-.01em}
.gh span{font-family:var(--mono);font-size:10px;color:var(--faint)}
.pk{display:grid;grid-template-columns:26px 1fr auto;gap:2px 12px;padding:9px 14px;
  align-items:center}
.pk+.pk{border-top:1px solid rgba(53,74,148,.25)}
.rank{font-family:var(--mono);font-size:14px;font-weight:800;color:var(--amberDim);text-align:center}
.nm{font-size:13.5px;font-weight:650;letter-spacing:-.01em}
.nm small{font-family:var(--mono);font-size:9.5px;color:var(--faint);font-weight:400;margin-left:6px}
.sub{grid-column:2/3;font-family:var(--mono);font-size:9.5px;color:var(--faint);
  display:flex;gap:10px;flex-wrap:wrap}
.big{font-family:var(--mono);font-size:20px;font-weight:800;letter-spacing:-.02em;
  grid-row:1/3;grid-column:3/4;text-align:right;min-width:62px}
.bar{grid-column:2/4;height:4px;background:var(--panel2);border-radius:100px;overflow:hidden;margin-top:3px}
.bar i{display:block;height:100%;background:var(--hot);border-radius:100px}
footer{padding:14px 20px 26px;font-family:var(--mono);font-size:10px;color:var(--faint);line-height:1.75}
footer b{color:var(--dim)}
</style></head><body><div class="wrap">
<header>
  <div class="eyebrow">MLB · BEST HIT BETS</div>
  <h1>Most Likely to Hit</h1>
  <div class="slate" id="slate"></div>
  <div id="stamps" style="margin-top:3px"></div>
</header>
<div class="nav">
  <a class="pill" href="games.html">Matchups</a>
  <a class="pill" href="players.html">Players</a>
  <a class="pill" href="daily.html">Stadium report</a>
  <button class="pill on" id="mode">By game</button>
</div>
<div id="board"></div>
<footer>
  A plate appearance is a walk, a hit by pitch, a strikeout, a homer, or a ball in play —
  only the last two can be a hit, so each is modelled separately and recombined as
  <b>P(HR) + P(in play) × BABIP</b>. Batter and pitcher are merged by <b>odds ratio</b>, and each
  rate is regressed by how fast it actually stabilises (strikeouts in ~60 PA, BABIP in many
  hundreds of balls in play). Pitching faced is <b>59% starter, 41% bullpen</b>.<br>
  Park and weather move homers and balls in play separately. Lineup slot sets plate appearances.
  <b>Projections, not predictions</b> — a 70% hitter still fails three nights in ten.
</footer></div>
<script>
const D = __DATA__;
let byGame = true;
function etTime(iso){ return new Date(iso).toLocaleString("en-US",{timeZone:"America/Chicago",
  hour:"numeric",minute:"2-digit",hour12:true}) + " CT"; }
function etDateShort(iso){ return new Date(iso).toLocaleDateString("en-US",
  {timeZone:"America/Chicago",month:"short",day:"numeric"}); }
function ago(iso){ const s=(Date.now()-new Date(iso).getTime())/1000;
  if(s<90) return "just now"; if(s<5400) return Math.round(s/60)+" min ago";
  if(s<172800) return Math.round(s/3600)+" hr ago"; return Math.round(s/86400)+" days ago"; }
function dayLabel(ymd){ const [y,m,d]=ymd.split("-").map(Number);
  return new Date(Date.UTC(y,m-1,d)).toLocaleDateString("en-US",
    {timeZone:"UTC",weekday:"long",month:"long",day:"numeric"}); }
function stamps(){
  const hrs=(Date.now()-new Date(D.built).getTime())/3.6e6;
  document.getElementById("stamps").innerHTML =
    `<span class="stamp${hrs>4?" stale":""}"><b>Updated</b> ${etTime(D.built)} on `+
    `${etDateShort(D.built)} · ${ago(D.built)}${hrs>4?" — may be out of date":""}</span>`;
}
function pick(c,i){
  return `<div class="pk">
    <span class="rank">${i+1}</span>
    <div class="nm">${c.name}<small>${c.bat} · ${c.team} · ${c.slot}${
      c.slot===1?"st":c.slot===2?"nd":c.slot===3?"rd":"th"}</small></div>
    <div class="big">${(c.p*100).toFixed(0)}%</div>
    <div class="sub"><span>${c.pa} PA</span><span>vs ${c.vs} (${c.hand})</span>
      <span>${(c.perPA*100).toFixed(0)}% per PA</span>${
        c.spd?`<span>${c.spd} ft/s</span>`:""}</div>
    <span class="bar"><i style="width:${(c.p*100).toFixed(0)}%"></i></span>
  </div>`;
}
function render(){
  const b=document.getElementById("board");
  if(byGame){
    b.innerHTML = D.games.map(g=>`<div class="game">
      <div class="gh"><b>${g.away} @ ${g.home}</b>
        <span>${etTime(g.start)}</span><span>${g.venue}</span>
        <span>${g.dome?"roof":(g.wx&&g.wx.mph!=null?`${Math.round(g.wx.mph)}mph ${g.row.replace(/_/g," ")}`:"")}</span>
      </div>${g.picks.map(pick).join("")}</div>`).join("")
      || `<div class="game"><div class="gh">No games scheduled.</div></div>`;
  } else {
    const all=D.games.flatMap(g=>g.picks.map(c=>({...c,g:`${g.away} @ ${g.home}`})))
      .sort((x,y)=>y.p-x.p);
    b.innerHTML = `<div class="game"><div class="gh"><b>Top picks across the slate</b>
      <span>${all.length} players</span></div>
      ${all.map((c,i)=>pick({...c,vs:c.vs},i)).join("")}</div>`;
  }
}
document.getElementById("slate").textContent =
  `${dayLabel(D.date)} · ${D.games.length} game${D.games.length===1?"":"s"} · top ${D.picks} per game`;
document.getElementById("mode").onclick=(e)=>{
  byGame=!byGame; e.target.textContent = byGame?"By game":"Ranked";
  render();
};
stamps(); setInterval(stamps,60000); render();
</script></body></html>
"""


if __name__ == "__main__":
    main()
