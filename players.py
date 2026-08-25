#!/usr/bin/env python3
"""
Daily player projections — hitters and starting pitchers, as prop probabilities.

Every rate a player brings into a game is built the same way:

    season rate -> weighted toward the last three weeks -> regressed to league

so a hot month moves the number and a hot week does not run away with it.
That rate is then pushed through three multipliers, each measured rather than
assumed:

    platoon    the batter's own split against this starter's hand, itself
               regressed toward the league platoon effect
    opponent   the starting pitcher's allowed rates, scaled by the share of
               plate appearances he is actually expected to take
    park       the HR and hit multipliers from the wind model in this repo,
               which already fold in tonight's forecast

Counting stats become probabilities exactly, not by simulation: plate
appearances are split between their two integer outcomes, hit and homer
chances are binomial across those, and total bases come from a small
convolution over the per-PA outcome distribution.

Runs and RBI are deliberately absent — both depend on teammates rather than
the batter, and a number that pretended otherwise would be worse than none.

    python3 players.py                  # today
    python3 players.py --date 2026-08-25
"""

import argparse
import json
import ssl
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # no tzdata
    ZoneInfo = None

import daily as Dl
import wind as W

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

API = "https://statsapi.mlb.com/api/v1"
UA = {"User-Agent": "Mozilla/5.0 (wind-profile)"}
CACHE = Path(__file__).parent / ".cache"

RECENT_DAYS = 21          # the "recent form" window
FORM_K = 150              # PA at which recent form carries half the weight
REG_PA = 100              # regression-to-league strength for a hitter's rate
PLATOON_REG = 120         # regression strength for a player's own platoon split
SP_PA_SHARE = 0.60        # share of a lineup's PAs the starter is expected to take
FLY_PER_PA = 0.14         # qualifying fly balls per plate appearance, league-wide

# Plate appearances by batting order slot, leadoff down to ninth.
SLOT_PA = [4.65, 4.54, 4.43, 4.32, 4.21, 4.10, 3.99, 3.88, 3.77]


def baseball_today():
    """MLB's day runs on Eastern time. A build at 00:30 UTC is still the
    previous evening's slate in the States, so never key off UTC."""
    tz = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc
    return datetime.now(tz).date()


def get(url, timeout=90, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def q(path, **params):
    return get(f"{API}/{path}?{urllib.parse.urlencode(params)}")


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def stat_map(splits, keys):
    """id -> {stat: value} for a bulk stats response."""
    out = {}
    for s in splits:
        pid = ((s.get("player") or {}).get("id"))
        if pid is None:
            continue
        st = s.get("stat") or {}
        row = {k: num(st.get(k)) for k in keys}
        prev = out.get(pid)
        if prev:  # traded players appear once per team; add the lines together
            for k in keys:
                row[k] += prev[k]
        out[pid] = row
    return out


HIT_KEYS = ("plateAppearances", "atBats", "hits", "doubles", "triples",
            "homeRuns", "baseOnBalls", "hitByPitch", "strikeOuts")
PIT_KEYS = ("battersFaced", "strikeOuts", "earnedRuns", "hits", "baseOnBalls",
            "homeRuns", "gamesStarted", "inningsPitched")


def blend(season, recent, key, denom, league):
    """Season rate, pulled toward recent form, then regressed to league."""
    sp = season.get(denom, 0.0)
    if sp <= 0:
        return league
    p_season = season.get(key, 0.0) / sp
    rp = (recent or {}).get(denom, 0.0)
    if rp > 0:
        p_recent = recent[key] / rp
        w = rp / (rp + FORM_K)
        p_form = (1 - w) * p_season + w * p_recent
    else:
        p_form = p_season
    return (sp * p_form + REG_PA * league) / (sp + REG_PA)


def platoon_mult(split_row, player_season, league_split_mult, key, denom):
    """How this batter fares against this hand, relative to himself."""
    if not split_row:
        return league_split_mult
    spa = split_row.get(denom, 0.0)
    base_pa = player_season.get(denom, 0.0)
    if spa <= 0 or base_pa <= 0:
        return league_split_mult
    base = player_season.get(key, 0.0) / base_pa
    if base <= 0:
        return league_split_mult
    raw = (split_row.get(key, 0.0) / spa) / base
    w = spa / (spa + PLATOON_REG)
    return w * raw + (1 - w) * league_split_mult


def pa_split(pa):
    """A fractional PA expectation as its two integer outcomes."""
    lo = int(pa)
    return [(lo, 1 - (pa - lo)), (lo + 1, pa - lo)]


def p_at_least_one(p, pa):
    return sum(wt * (1 - (1 - p) ** n) for n, wt in pa_split(pa))


def p_at_least_two(p, pa):
    tot = 0.0
    for n, wt in pa_split(pa):
        none = (1 - p) ** n
        one = n * p * (1 - p) ** (n - 1) if n else 0.0
        tot += wt * max(0.0, 1 - none - one)
    return tot


def p_tb_at_least(p1, p2, p3, p4, pa, target):
    """Exact via convolution over the per-PA total-base distribution."""
    per = {0: max(0.0, 1 - p1 - p2 - p3 - p4), 1: p1, 2: p2, 3: p3, 4: p4}
    tot = 0.0
    for n, wt in pa_split(pa):
        dist = {0: 1.0}
        for _ in range(n):
            nxt = defaultdict(float)
            for tb, pr in dist.items():
                for add, pa_pr in per.items():
                    nxt[min(tb + add, target)] += pr * pa_pr
            dist = nxt
        tot += wt * dist.get(target, 0.0)
    return tot


def binom_at_least(n, p, k):
    n = int(round(n))
    if n <= 0:
        return 0.0
    # complement of the lower tail, computed iteratively to avoid factorials
    term = (1 - p) ** n
    cum = term
    for i in range(k - 1):
        if i + 1 > n:
            break
        term *= (n - i) / (i + 1) * (p / (1 - p) if p < 1 else 0)
        cum += term
    return max(0.0, min(1.0, 1 - cum))


def recent_lineups(day):
    """Each team's most recently posted lineup, for games without one yet."""
    start = (day - timedelta(days=10)).isoformat()
    j = q("schedule", sportId=1, startDate=start, endDate=day.isoformat(),
          hydrate="lineups,team", gameType="R")
    latest = {}
    for d in j.get("dates", []):
        for g in d.get("games", []):
            lu = g.get("lineups") or {}
            for side, key in (("home", "homePlayers"), ("away", "awayPlayers")):
                players = lu.get(key) or []
                tid = ((g["teams"][side].get("team") or {}).get("id"))
                if tid and players:
                    latest[tid] = [p["id"] for p in players]
    return latest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=baseball_today().isoformat())
    ap.add_argument("--out", default="players.html")
    a = ap.parse_args()
    day = date.fromisoformat(a.date)
    season = day.year
    since = (day - timedelta(days=RECENT_DAYS)).isoformat()

    common = dict(season=season, sportId=1, gameType="R", limit=2000, playerPool="All")
    print("pulling league stats …", flush=True)
    h_season = stat_map(q("stats", stats="season", group="hitting", **common)
                        ["stats"][0]["splits"], HIT_KEYS)
    h_recent = stat_map(q("stats", stats="byDateRange", group="hitting",
                          startDate=since, endDate=a.date, **common)
                        ["stats"][0]["splits"], HIT_KEYS)
    p_season = stat_map(q("stats", stats="season", group="pitching", **common)
                        ["stats"][0]["splits"], PIT_KEYS)
    p_recent = stat_map(q("stats", stats="byDateRange", group="pitching",
                          startDate=since, endDate=a.date, **common)
                        ["stats"][0]["splits"], PIT_KEYS)
    splits = {}
    for code, hand in (("vl", "L"), ("vr", "R")):
        splits[hand] = stat_map(q("stats", stats="statSplits", sitCodes=code,
                                  group="hitting", **common)["stats"][0]["splits"], HIT_KEYS)
    print(f"  {len(h_season)} hitters, {len(p_season)} pitchers", flush=True)

    # League baselines and the average platoon effect, from the same pulls.
    tot = defaultdict(float)
    for r in h_season.values():
        for k in HIT_KEYS:
            tot[k] += r[k]
    LG = {k: tot[k] / tot["plateAppearances"] for k in HIT_KEYS if k != "plateAppearances"}
    lg_split = {}
    for hand, tbl in splits.items():
        s = defaultdict(float)
        for r in tbl.values():
            for k in HIT_KEYS:
                s[k] += r[k]
        lg_split[hand] = {k: (s[k] / s["plateAppearances"]) / LG[k] if LG.get(k) else 1.0
                          for k in HIT_KEYS if k != "plateAppearances"}
    ptot = defaultdict(float)
    for r in p_season.values():
        for k in PIT_KEYS:
            ptot[k] += r[k]
    LGP = {k: ptot[k] / ptot["battersFaced"] for k in PIT_KEYS if k != "battersFaced"}

    # Park and weather, straight from the wind model already in this repo.
    print("building park/weather model …", flush=True)
    winds, rows = Dl.load_history()
    park_delta, _pc, temp_delta, fb_per_game = Dl.build_model(rows)
    domes = Dl.dome_parks(winds)
    orient = json.loads((CACHE / "orient.json").read_text()) if (CACHE / "orient.json").exists() else {}

    sched = q("schedule", sportId=1, date=a.date,
              hydrate="probablePitcher,lineups,team,venue(location)")
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    print(f"{len(games)} games on {a.date}", flush=True)
    fallback_lu = recent_lineups(day)

    # Handedness for everyone involved, one roster call per club.
    hands = {}
    for g in games:
        for side in ("home", "away"):
            tid = g["teams"][side]["team"]["id"]
            if tid in hands:
                continue
            hands[tid] = {}
            try:
                for e in q(f"teams/{tid}/roster", rosterType="active",
                           hydrate="person")["roster"]:
                    p = e["person"]
                    hands[tid][p["id"]] = {
                        "bat": (p.get("batSide") or {}).get("code", "R"),
                        "throw": (p.get("pitchHand") or {}).get("code", "R"),
                        "name": p.get("fullName", ""),
                    }
            except Exception:  # noqa: BLE001
                pass
    who = {pid: v for t in hands.values() for pid, v in t.items()}

    out = []
    for g in games:
        ven = g.get("venue") or {}
        vid = ven.get("id")
        crd = ((ven.get("location") or {}).get("defaultCoordinates") or {})
        wx, row, band = Dl.forecast_cell(g, vid, crd, domes, orient)
        chain = [f"{vid}|{row}|{band}", f"{vid}|{row}|*", f"{vid}|light|*"]
        src = next((c for c in chain if c in park_delta), None)
        pd = park_delta.get(src) or {k: 0.0 for k in Dl.OUTCOMES}
        # Per-fly-ball edges become rate multipliers on a per-PA basis.
        hr_mult = 1 + (pd["hr"] * FLY_PER_PA) / LG["homeRuns"] if LG["homeRuns"] else 1
        hit_mult = 1 + (sum(pd[k] for k in Dl.OUTCOMES) * FLY_PER_PA) / LG["hits"] if LG["hits"] else 1

        lu = g.get("lineups") or {}
        game_rows = {"venue": ven.get("name", ""), "start": g.get("gameDate", ""),
                     "status": g.get("status", {}).get("detailedState", ""),
                     "wx": wx, "row": row, "dome": vid in domes,
                     "hrMult": round(hr_mult, 3), "hitMult": round(hit_mult, 3),
                     "sides": []}

        for side, other, key in (("away", "home", "awayPlayers"),
                                 ("home", "away", "homePlayers")):
            team = g["teams"][side]["team"]
            opp_sp = (g["teams"][other].get("probablePitcher") or {})
            sp_id = opp_sp.get("id")
            sp_hand = (who.get(sp_id, {}) or {}).get("throw", "R")
            sp_row = p_season.get(sp_id)

            # Opponent multipliers, damped by how much of the game he'll pitch.
            def sp_mult(stat):
                if not sp_row or sp_row.get("battersFaced", 0) < 100:
                    return 1.0
                raw = (sp_row[stat] / sp_row["battersFaced"]) / LGP[stat] if LGP.get(stat) else 1.0
                return 1 + SP_PA_SHARE * (raw - 1)

            m_hit, m_hr = sp_mult("hits"), sp_mult("homeRuns")

            ids = [p["id"] for p in (lu.get(key) or [])]
            confirmed = bool(ids)
            if not ids:
                ids = fallback_lu.get(team["id"], [])[:9]

            batters = []
            for slot, pid in enumerate(ids[:9]):
                s, r = h_season.get(pid), h_recent.get(pid)
                if not s or s.get("plateAppearances", 0) < 30:
                    continue
                pa = SLOT_PA[slot]
                sp_tbl = splits.get(sp_hand, {})
                srow = sp_tbl.get(pid)

                def rate(k):
                    base = blend(s, r, k, "plateAppearances", LG[k])
                    pm = platoon_mult(srow, s, lg_split[sp_hand].get(k, 1.0),
                                      k, "plateAppearances")
                    return base * pm

                p_h = rate("hits") * m_hit * hit_mult
                p_hr = rate("homeRuns") * m_hr * hr_mult
                p_2b, p_3b = rate("doubles"), rate("triples")
                p_1b = max(0.0, p_h - p_2b - p_3b - p_hr)
                batters.append({
                    "id": pid, "slot": slot + 1,
                    "name": (who.get(pid, {}) or {}).get("name") or str(pid),
                    "bat": (who.get(pid, {}) or {}).get("bat", "R"),
                    "pa": round(pa, 1),
                    "h1": round(p_at_least_one(p_h, pa), 3),
                    "h2": round(p_at_least_two(p_h, pa), 3),
                    "hr": round(p_at_least_one(p_hr, pa), 3),
                    "tb2": round(p_tb_at_least(p_1b, p_2b, p_3b, p_hr, pa, 2), 3),
                })

            pitcher = None
            own_sp = (g["teams"][side].get("probablePitcher") or {})
            ps = p_season.get(own_sp.get("id"))
            if ps and ps.get("battersFaced", 0) >= 60:
                pr = p_recent.get(own_sp["id"])
                gs = max(1.0, ps.get("gamesStarted", 0) or 1.0)
                bf = min(28.0, max(12.0, ps["battersFaced"] / gs))
                k_rate = blend(ps, pr, "strikeOuts", "battersFaced", LGP["strikeOuts"])
                er_rate = blend(ps, pr, "earnedRuns", "battersFaced", LGP["earnedRuns"])
                pitcher = {
                    "id": own_sp["id"],
                    "name": own_sp.get("fullName", ""),
                    "hand": (who.get(own_sp["id"], {}) or {}).get("throw", "R"),
                    "bf": round(bf, 1),
                    "k": round(k_rate * bf, 1),
                    "k5": round(binom_at_least(bf, k_rate, 5), 3),
                    "k6": round(binom_at_least(bf, k_rate, 6), 3),
                    "er": round(er_rate * bf * hr_mult ** 0.5, 2),
                }

            game_rows["sides"].append({
                "team": team.get("abbreviation", ""),
                "opp_sp": opp_sp.get("fullName", ""),
                "opp_hand": sp_hand,
                "confirmed": confirmed,
                "batters": batters,
                "pitcher": pitcher,
            })
        out.append(game_rows)

    out.sort(key=lambda g: g.get("start") or "")  # chronological, like a schedule
    payload = {"games": out, "date": a.date,
               "built": datetime.now(timezone.utc).isoformat(timespec="minutes"),
               "confirmed": sum(1 for g in out for si in g["sides"] if si["confirmed"]),
               "sides": sum(len(g["sides"]) for g in out)}
    Path(a.out).write_text(
        TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"))),
        encoding="utf-8")
    n = sum(len(s["batters"]) for g in out for s in g["sides"])
    print(f"wrote {a.out} ({n} hitters projected)")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Player Projections</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --hot:#31D07E; --cold:#FF4D6A;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
header{padding:20px 20px 10px}
.eyebrow{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--amber)}
h1{margin:2px 0 0;font-size:26px;font-weight:800;letter-spacing:-.035em}
.meta{font-family:var(--mono);font-size:10px;color:var(--faint);margin-top:5px}
.slate{font-size:13px;font-weight:600;color:var(--text);margin-top:4px;letter-spacing:-.01em}
.stamps{display:flex;gap:6px 14px;flex-wrap:wrap;margin-top:4px}
.stamp{font-family:var(--mono);font-size:10px;color:var(--faint);letter-spacing:.03em}
.stamp b{color:var(--dim);font-weight:600}
.stamp.stale{color:var(--amber)}
.stamp.stale b{color:var(--amber)}

.nav{padding:0 20px 12px;display:flex;gap:8px;flex-wrap:wrap}
.pill{padding:6px 12px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--amber);
  background:var(--panel);border:1px solid var(--amberDim);border-radius:100px;cursor:pointer;
  text-decoration:none;display:inline-block}
.pill:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
.pill.on{background:var(--amber);color:var(--ink);border-color:var(--amber)}
.game{margin:0 20px 8px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.game[open]{border-color:var(--line2)}
summary.gh{list-style:none;cursor:pointer;user-select:none;padding:10px 14px;
  display:grid;grid-template-columns:12px minmax(96px,auto) 78px minmax(0,1fr) auto auto;
  gap:2px 12px;align-items:center}
summary.gh::-webkit-details-marker{display:none}
summary.gh:hover{background:rgba(53,74,148,.18)}
.game[open] summary.gh{border-bottom:1px solid var(--line)}
.caret{color:var(--faint);font-size:10px;transition:transform .12s;display:inline-block}
.game[open] .caret{transform:rotate(90deg)}
.mu{font-size:15px;font-weight:700;letter-spacing:-.015em;white-space:nowrap}
.gt{font-family:var(--mono);font-size:10px;color:var(--dim);white-space:nowrap}
.vn{font-family:var(--mono);font-size:10px;color:var(--faint);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.gwx{font-family:var(--mono);font-size:10px;color:var(--faint);white-space:nowrap;text-align:right}
.lu{font-family:var(--mono);font-size:9px;padding:1px 6px;border-radius:100px;
  border:1px solid var(--line2);color:var(--faint);white-space:nowrap}
.lu.ok{border-color:#1F7A4C;color:var(--hot)}
@media(max-width:680px){
  summary.gh{grid-template-columns:12px 1fr auto;row-gap:3px}
  .gt{text-align:right}
  .vn{grid-column:2/4}
  .gwx{grid-column:2/4;text-align:left}
  .lu{grid-column:2/4;justify-self:start}
}
.gh{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;
  justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}
.gh b{font-size:14px;letter-spacing:-.01em}
.gh span{font-family:var(--mono);font-size:10px;color:var(--faint)}
.side{padding:6px 0}
.sh{padding:6px 14px;font-family:var(--mono);font-size:10px;color:var(--dim);
  display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tag{font-size:9px;padding:1px 6px;border-radius:100px;border:1px solid var(--line2);color:var(--faint)}
.tag.ok{border-color:#1F7A4C;color:var(--hot)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:560px}
th,td{padding:5px 8px;font-family:var(--mono);font-size:11px;font-variant-numeric:tabular-nums;
  text-align:right;white-space:nowrap}
th{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--amber);
  border-bottom:1px solid var(--line);font-weight:600}
th.l,td.l{text-align:left}
td.nm{font-size:12px;font-weight:600;color:var(--text)}
td.nm small{color:var(--faint);font-weight:400;margin-left:5px}
tbody tr+tr td{border-top:1px solid rgba(53,74,148,.25)}
.p{font-weight:700}
.sp{padding:7px 14px;font-family:var(--mono);font-size:11px;color:var(--dim);
  border-top:1px solid var(--line);display:flex;gap:14px;flex-wrap:wrap;align-items:baseline}
.sp b{color:var(--text);font-size:12px}
footer{padding:14px 20px 26px;font-family:var(--mono);font-size:10px;color:var(--faint);line-height:1.75}
footer b{color:var(--dim)}
</style></head><body><div class="wrap">
<header>
  <div class="eyebrow">MLB · DAILY PROJECTIONS</div>
  <h1>Player Projections</h1>
  <div class="slate" id="slate"></div>
  <div class="stamps" id="stamps"></div>
</header>
<div class="nav">
  <a class="pill" href="daily.html">&larr; Stadium report</a>
  <a class="pill" href="index.html">Wind profile</a>
  <button class="pill on" id="sortBy" data-k="h1">Sort: 1+ Hit</button>
  <button class="pill" id="expand" type="button">Expand all</button>
</div>
<div id="board"></div>
<footer>
  Each rate blends season-long production with the last <b>21 days</b>, regressed toward league
  average, then adjusted for the batter's platoon split against tonight's starter, that starter's
  own allowed rates, and the park and weather multipliers from the wind model.<br>
  <b>Confirmed</b> lineups come from MLB; <b>projected</b> ones reuse the club's last posted card and
  will move when the real one drops. Runs and RBI are omitted on purpose — they depend on teammates,
  not the hitter. <b>Projections, not predictions.</b>
</footer></div>
<script>
const D = __DATA__;
const COLS=[["h1","1+ H"],["h2","2+ H"],["tb2","2+ TB"],["hr","1+ HR"]];
let sortK="h1";

function pc(v){ return (v*100).toFixed(0)+"%"; }
function shade(v,lo,hi){
  const t=Math.max(0,Math.min(1,(v-lo)/(hi-lo)));
  return `color:rgb(${Math.round(140+115*t)},${Math.round(154+114*t)},${Math.round(199+56*t)})`;
}
function side(s){
  const bs=[...s.batters].sort((a,b)=>b[sortK]-a[sortK]);
  if(!bs.length) return `<div class="side"><div class="sh"><b>${s.team}</b>
    <span class="tag">no lineup yet</span></div></div>`;
  const rows=bs.map(b=>`<tr>
    <td class="l nm">${b.slot}. ${b.name}<small>${b.bat}</small></td>
    <td>${b.pa}</td>
    ${COLS.map(([k])=>`<td class="p" style="${shade(b[k],.05,.75)}">${pc(b[k])}</td>`).join("")}
  </tr>`).join("");
  const p=s.pitcher;
  return `<div class="side">
    <div class="sh"><b style="color:var(--text);font-size:12px">${s.team}</b>
      <span class="tag ${s.confirmed?'ok':''}">${s.confirmed?'confirmed':'projected'}</span>
      <span>vs ${s.opp_sp||"TBD"} (${s.opp_hand})</span></div>
    <div class="scroll"><table><thead><tr>
      <th class="l">Batter</th><th>PA</th>${COLS.map(([,l])=>`<th>${l}</th>`).join("")}
    </tr></thead><tbody>${rows}</tbody></table></div>
    ${p?`<div class="sp"><b>${p.name}</b> (${p.hand}) &middot; ${p.bf} BF
      &middot; <b>${p.k}</b> K &middot; 5+K ${pc(p.k5)} &middot; 6+K ${pc(p.k6)}
      &middot; ${p.er} ER</div>`:""}
  </div>`;
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

// Open panels are tracked by index so re-sorting doesn't collapse them.
const openSet = new Set();
function gameTime(iso){
  if(!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US",{timeZone:"America/New_York",
    hour:"numeric",minute:"2-digit"}) + " ET";
}
function summaryOf(g){
  const away=g.sides[0]||{}, home=g.sides[1]||{};
  const mu = (away.team||"?") + " @ " + (home.team||"?");
  const conf = g.sides.filter(s=>s.confirmed).length;
  const wx = g.dome ? "roof"
    : (g.wx && g.wx.mph!=null
        ? `${Math.round(g.wx.mph)}mph ${g.row.replace(/_/g," ")} · ${Math.round(g.wx.temp)}°`
        : "no forecast");
  return `<summary class="gh">
    <span class="caret">&#9654;</span>
    <span class="mu">${mu}</span>
    <span class="gt">${gameTime(g.start)}</span>
    <span class="vn">${g.venue}</span>
    <span class="gwx">${wx} · HR &times;${g.hrMult.toFixed(2)}</span>
    <span class="lu ${conf===2?"ok":""}">${conf}/2 lineups</span>
  </summary>`;
}
function render(){
  const board=document.getElementById("board");
  board.innerHTML = D.games.length ? D.games.map((g,i)=>
    `<details class="game" data-i="${i}"${openSet.has(i)?" open":""}>
       ${summaryOf(g)}${g.sides.map(side).join("")}
     </details>`).join("")
    : `<div class="game"><div style="padding:14px">No games scheduled.</div></div>`;
  board.querySelectorAll("details.game").forEach(d=>{
    d.addEventListener("toggle",()=>{
      const i=+d.dataset.i;
      d.open ? openSet.add(i) : openSet.delete(i);
      syncExpandLabel();
    });
  });
  syncExpandLabel();
}
function syncExpandLabel(){
  const btn=document.getElementById("expand");
  if(btn) btn.textContent = openSet.size >= D.games.length && D.games.length
    ? "Collapse all" : "Expand all";
}
document.getElementById("slate").textContent =
  `${dayLabel(D.date)} · ${D.games.length} game${D.games.length===1?"":"s"}`;
startStampTicker(()=>{ document.getElementById("stamps").innerHTML =
  stampHTML(D.built, `${D.confirmed}/${D.sides} lineups confirmed`); });

document.getElementById("expand").onclick=()=>{
  if(openSet.size >= D.games.length){ openSet.clear(); }
  else { D.games.forEach((_,i)=>openSet.add(i)); }
  render();
};
const btn=document.getElementById("sortBy");
btn.onclick=()=>{ const i=COLS.findIndex(c=>c[0]===sortK);
  const nx=COLS[(i+1)%COLS.length]; sortK=nx[0]; btn.textContent="Sort: "+nx[1]; render(); };
render();
</script></body></html>
"""


if __name__ == "__main__":
    main()
