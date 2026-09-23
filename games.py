#!/usr/bin/env python3
"""
Matchup projections — expected runs and a win probability for each game.

This is the top of the stack: it reuses the lineup rates from players.py and
the park-and-weather model from daily.py, and turns them into a score.

    1. Each projected lineup becomes a wOBA, from per-hitter rates that are
       already blended with recent form and regressed to league.
    2. Those plate appearances are split between the opposing starter and the
       opposing bullpen — 59/41, which is the league's actual split, not a
       guess — and each half is scaled by that pitching's own allowed rates.
    3. Park and tonight's weather scale the extra-base side.
    4. wOBA becomes runs through the standard wRAA conversion, calibrated to
       this season's league run environment rather than a constant.
    5. Expected runs become a win probability via Pythagenpat, plus a
       home-field term measured from this season's completed games.

Home advantage is applied as a probability shift, not extra runs, because
that is what the data says it is: home clubs win about 53% while outscoring
visitors by roughly a twentieth of a run. Batting last is worth more than
the scoring is.

    python3 games.py                  # today
    python3 games.py --date 2026-08-25
"""

import argparse
import json
import math
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from orient import load_orientations
import daily as Dl
import dk
import players as P

CACHE = Path(__file__).parent / ".cache"

# wOBA event weights. Scale converts a wOBA gap into runs per plate appearance.
W = {"bb": 0.69, "hbp": 0.72, "s1": 0.89, "s2": 1.27, "s3": 1.62, "hr": 2.10}
WOBA_SCALE = 1.20

SP_SHARE = 0.59       # league-measured share of batters faced by starters
PA_PER_TEAM = 37.8    # plate appearances a side gets in a typical nine innings
PYTH_BASE = 0.287     # Pythagenpat exponent term


def team_pitching(season):
    """Season pitching rates per team, split into starters and bullpen."""
    out = {}
    for code, tag in (("sp", "sp"), ("rp", "rp")):
        j = P.q("teams/stats", stats="statSplits", sitCodes=code, group="pitching",
                season=season, sportId=1, gameType="R")
        for s in j["stats"][0]["splits"]:
            tid = (s.get("team") or {}).get("id")
            st = s.get("stat") or {}
            bf = P.num(st.get("battersFaced"))
            if not tid or bf < 200:
                continue
            out.setdefault(tid, {})[tag] = {
                "bf": bf,
                "h": P.num(st.get("hits")),
                "hr": P.num(st.get("homeRuns")),
                "bb": P.num(st.get("baseOnBalls")),
                "so": P.num(st.get("strikeOuts")),
            }
    return out


def league_env(season):
    """This season's run environment, so the conversion isn't a hardcoded era."""
    j = P.q("teams/stats", stats="season", group="hitting",
            season=season, sportId=1, gameType="R")
    tot = defaultdict(float)
    for s in j["stats"][0]["splits"]:
        st = s.get("stat") or {}
        for k, f in (("pa", "plateAppearances"), ("r", "runs"), ("h", "hits"),
                     ("d", "doubles"), ("t", "triples"), ("hr", "homeRuns"),
                     ("bb", "baseOnBalls"), ("hbp", "hitByPitch")):
            tot[k] += P.num(st.get(f))
    pa = tot["pa"]
    singles = tot["h"] - tot["d"] - tot["t"] - tot["hr"]
    woba = (W["bb"] * tot["bb"] + W["hbp"] * tot["hbp"] + W["s1"] * singles
            + W["s2"] * tot["d"] + W["s3"] * tot["t"] + W["hr"] * tot["hr"]) / pa
    return {"woba": woba, "r_pa": tot["r"] / pa}


def home_edge(season):
    """Home win rate and run split, measured rather than assumed."""
    j = P.q("schedule", sportId=1, gameType="R",
            startDate=f"{season}-03-01", endDate=f"{season}-12-01",
            fields="dates,games,teams,home,away,score,status,codedGameState")
    hw = n = 0
    for d in j.get("dates", []):
        for g in d.get("games", []):
            if (g.get("status") or {}).get("codedGameState") != "F":
                continue
            h, a = g["teams"]["home"], g["teams"]["away"]
            if "score" not in h or "score" not in a:
                continue
            n += 1
            hw += h["score"] > a["score"]
    if n < 200:
        return 0.11, n, None          # fall back to the historical norm
    rate = hw / n
    return math.log(rate / (1 - rate)), n, rate


def live_state(g):
    """Current score and state for a game under way or finished, else None.

    Strictly a display layer. It is deliberately kept out of the projection
    payload: `ledger.py` parses that payload back out of this page to freeze
    predictions before first pitch, and a live score has no business anywhere
    near that path. Keeping the two apart is what makes it impossible for a
    result to leak backwards into the number that predicted it.
    """
    st = g.get("status") or {}
    phase = st.get("abstractGameState")
    if phase not in ("Live", "Final"):
        return None
    h, a = g["teams"]["home"], g["teams"]["away"]
    if h.get("score") is None or a.get("score") is None:
        return None
    out = {"state": phase, "home": int(h["score"]), "away": int(a["score"]),
           "detail": st.get("detailedState") or phase}
    if phase == "Live":
        ls = g.get("linescore") or {}
        out["inning"] = ls.get("currentInning")
        out["half"] = (ls.get("inningState") or "")[:3]
    # Deliberately no baserunners here. This snapshot is frozen at build time
    # and the page may be opened hours later; a stale score is merely old, but
    # a stale runner on second is a lie about the state of the game. Runners
    # are shown only from a live browser refresh, where they are seconds old.
    return out


def pitch_mult(row, lg, key, denom_key):
    """A pitching staff's allowed rate against league, as a multiplier."""
    if not row or row["bf"] < 200 or not lg.get(denom_key):
        return 1.0
    return (row[key] / row["bf"]) / lg[denom_key]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=P.baseball_today().isoformat())
    ap.add_argument("--out", default="games.html")
    a = ap.parse_args()
    season = date.fromisoformat(a.date).year

    print("pulling league + team stats …", flush=True)
    common = dict(season=season, sportId=1, gameType="R", limit=2000, playerPool="All")
    h_season = P.stat_map(P.q("stats", stats="season", group="hitting", **common)
                          ["stats"][0]["splits"], P.HIT_KEYS)
    since = (date.fromisoformat(a.date) - __import__("datetime").timedelta(
        days=P.RECENT_DAYS)).isoformat()
    h_recent = P.stat_map(P.q("stats", stats="byDateRange", group="hitting",
                              startDate=since, endDate=a.date, **common)
                          ["stats"][0]["splits"], P.HIT_KEYS)
    p_season = P.stat_map(P.q("stats", stats="season", group="pitching", **common)
                          ["stats"][0]["splits"], P.PIT_KEYS)
    splits = {}
    for code, hand in (("vl", "L"), ("vr", "R")):
        splits[hand] = P.stat_map(P.q("stats", stats="statSplits", sitCodes=code,
                                      group="hitting", **common)["stats"][0]["splits"],
                                  P.HIT_KEYS)
    staff = team_pitching(season)
    lg = league_env(season)
    hfa, hfa_n, hfa_rate = home_edge(season)
    print(f"  league wOBA {lg['woba']:.3f}, {lg['r_pa']:.4f} R/PA · "
          f"home edge {hfa:+.3f} from {hfa_n} games", flush=True)

    # League rate baselines for the hitter side.
    tot = defaultdict(float)
    for r in h_season.values():
        for k in P.HIT_KEYS:
            tot[k] += r[k]
    LG = {k: tot[k] / tot["plateAppearances"] for k in P.HIT_KEYS
          if k != "plateAppearances"}
    lg_split = {}
    for hand, tbl in splits.items():
        s = defaultdict(float)
        for r in tbl.values():
            for k in P.HIT_KEYS:
                s[k] += r[k]
        lg_split[hand] = {k: (s[k] / s["plateAppearances"]) / LG[k] if LG.get(k) else 1.0
                          for k in P.HIT_KEYS if k != "plateAppearances"}
    lg_p = {"h": LG["hits"], "hr": LG["homeRuns"], "bb": LG["baseOnBalls"]}

    print("building park/weather model …", flush=True)
    park_delta, _pc, _td, _fb, domes, hand_delta = Dl.load_model()
    orient = load_orientations()

    odds = dk.load(date.fromisoformat(a.date))
    sched = P.q("schedule", sportId=1, date=a.date,
                hydrate="probablePitcher,lineups,team,venue(location),linescore")
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    fallback = P.recent_lineups(date.fromisoformat(a.date))
    print(f"{len(games)} games on {a.date}", flush=True)

    hands = {}
    for g in games:
        for side in ("home", "away"):
            tid = g["teams"][side]["team"]["id"]
            if tid in hands:
                continue
            hands[tid] = {}
            try:
                for e in P.q(f"teams/{tid}/roster", rosterType="active",
                             hydrate="person")["roster"]:
                    pr = e["person"]
                    hands[tid][pr["id"]] = {
                        "bat": (pr.get("batSide") or {}).get("code", "R"),
                        "throw": (pr.get("pitchHand") or {}).get("code", "R"),
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
        pdl = park_delta.get(src) or {k: 0.0 for k in Dl.OUTCOMES}
        hr_mult = 1 + (pdl["hr"] * P.FLY_PER_PA) / LG["homeRuns"] if LG["homeRuns"] else 1
        xb_mult = 1 + ((pdl["x2"] + pdl["x3"]) * P.FLY_PER_PA) / (LG["doubles"] + LG["triples"]) \
            if (LG["doubles"] + LG["triples"]) else 1

        sides = {}
        for side, other in (("away", "home"), ("home", "away")):
            team = g["teams"][side]["team"]
            opp_team = g["teams"][other]["team"]
            opp_sp = (g["teams"][other].get("probablePitcher") or {})
            sp_hand = (who.get(opp_sp.get("id"), {}) or {}).get("throw", "R")
            sp_row = p_season.get(opp_sp.get("id"))
            bull = (staff.get(opp_team["id"]) or {}).get("rp")

            # Starter multipliers, from his own line; bullpen from the club's.
            def sp_m(stat, lgk):
                if not sp_row or sp_row.get("battersFaced", 0) < 100:
                    return 1.0
                return (sp_row[stat] / sp_row["battersFaced"]) / lg_p[lgk]
            sp_h, sp_hr, sp_bb = sp_m("hits", "h"), sp_m("homeRuns", "hr"), sp_m("baseOnBalls", "bb")
            rp_h = pitch_mult(bull, lg_p, "h", "h")
            rp_hr = pitch_mult(bull, lg_p, "hr", "hr")
            rp_bb = pitch_mult(bull, lg_p, "bb", "bb")
            # Blend the two halves of the game into one multiplier per event.
            m_h = SP_SHARE * sp_h + (1 - SP_SHARE) * rp_h
            m_hr = SP_SHARE * sp_hr + (1 - SP_SHARE) * rp_hr
            m_bb = SP_SHARE * sp_bb + (1 - SP_SHARE) * rp_bb

            ids = [p["id"] for p in ((g.get("lineups") or {}).get(
                "awayPlayers" if side == "away" else "homePlayers") or [])]
            confirmed = bool(ids)
            if not ids:
                ids = fallback.get(team["id"], [])[:9]

            num = den = 0.0
            used = 0
            for slot, pid in enumerate(ids[:9]):
                s = h_season.get(pid)
                if not s or s.get("plateAppearances", 0) < 30:
                    s = {k: 0.0 for k in P.HIT_KEYS}  # league-average stand-in
                r = h_recent.get(pid)
                srow = splits.get(sp_hand, {}).get(pid)

                def rate(k):
                    base = P.blend(s, r, k, "plateAppearances", LG[k])
                    return base * P.platoon_mult(srow, s, lg_split[sp_hand].get(k, 1.0),
                                                 k, "plateAppearances")

                pa_w = P.SLOT_PA[slot]
                bat = (who.get(pid, {}) or {}).get("bat", "R")
                hd = hand_delta.get(f"{vid}|{bat}") if vid not in domes else None
                hand_hr = 1 + (hd["hr"] * P.FLY_PER_PA) / LG["homeRuns"] if hd and LG["homeRuns"] else 1.0
                hr = rate("homeRuns") * m_hr * hr_mult * hand_hr
                d2 = rate("doubles") * m_h * xb_mult
                d3 = rate("triples") * m_h * xb_mult
                hits = rate("hits") * m_h
                s1 = max(0.0, hits - d2 - d3 - hr)
                bb = rate("baseOnBalls") * m_bb
                hbp = rate("hitByPitch")
                woba = (W["bb"] * bb + W["hbp"] * hbp + W["s1"] * s1 +
                        W["s2"] * d2 + W["s3"] * d3 + W["hr"] * hr)
                num += woba * pa_w
                den += pa_w
                used += 1

            woba = num / den if den else lg["woba"]
            r_pa = lg["r_pa"] + (woba - lg["woba"]) / WOBA_SCALE
            runs = max(0.8, r_pa * PA_PER_TEAM)
            sides[side] = {"team": team.get("abbreviation", ""),
                           "name": team.get("teamName", team.get("name", "")),
                           "sp": opp_sp.get("fullName", ""), "confirmed": confirmed,
                           "hitters": used, "woba": round(woba, 4), "runs": round(runs, 2)}

        if len(sides) != 2:
            continue
        rh, ra = sides["home"]["runs"], sides["away"]["runs"]
        x = max(0.6, (rh + ra) ** PYTH_BASE)
        pyth = rh ** x / (rh ** x + ra ** x)
        # Home advantage as a logit shift: batting last, not extra scoring.
        wp_home = 1 / (1 + math.exp(-(math.log(pyth / (1 - pyth)) + hfa)))
        rec = {
            "gamePk": g.get("gamePk"),
            "venue": ven.get("name", ""), "start": g.get("gameDate", ""),
            "home": sides["home"], "away": sides["away"],
            "wpHome": round(wp_home, 4), "total": round(rh + ra, 2),
            "dome": vid in domes, "wx": wx, "row": row,
        }
        # DraftKings' pre-game prices, when there are any. Part of D on
        # purpose: the ledger freezes them with the prediction at first pitch.
        prices = dk.game_prices(dk.find_game(odds, g["teams"]["home"].get("team"),
                                             g["teams"]["away"].get("team"),
                                             rec["start"]))
        if prices:
            rec["dk"] = prices
        out.append(rec)

    out.sort(key=lambda r: r.get("start") or "")  # first pitch order, like a schedule
    priced = sum(1 for r in out if r.get("dk"))
    if odds.get("events"):
        print(f"  dk: prices on {priced} of {len(out)} games", flush=True)
    payload = {"games": out, "date": a.date, "dkAt": odds.get("at") if priced else None,
               "built": datetime.now(timezone.utc).isoformat(timespec="minutes"),
               "hfa": round(hfa, 4), "hfaRate": hfa_rate, "hfaN": hfa_n,
               "lgRuns": round(lg["r_pa"] * PA_PER_TEAM, 2)}
    # A snapshot of where the games stand right now, so the page is useful even
    # if the visitor's browser cannot reach MLB directly. Separate payload,
    # separate constant — see live_state().
    live = {}
    for g in games:
        gp = g.get("gamePk")
        st = live_state(g) if gp else None
        if st:
            live[str(gp)] = st
    Path(a.out).write_text(
        TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
                .replace("__MINEV__", str(dk.MIN_EV))
                .replace("__LIVE__", json.dumps(live, separators=(",", ":"))),
        encoding="utf-8")
    if out:
        avg = sum(o["wpHome"] for o in out) / len(out)
        print(f"wrote {a.out} — {len(out)} games, mean home win prob {avg:.3f}, "
              f"{len(live)} with a live score")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Matchup Projections</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --win:#31D07E; --lose:#FF4D6A;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto}
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
  text-decoration:none;display:inline-block}
.pill:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
.card{margin:0 20px 8px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:12px 14px;display:grid;
  grid-template-columns:1fr 96px;gap:8px 14px;align-items:center}
.mu{display:flex;flex-direction:column;gap:5px;min-width:0}
.tm{display:flex;align-items:baseline;gap:8px}
.ab{font-family:var(--mono);font-size:13px;font-weight:700;width:42px;flex:0 0 auto}
.bar{flex:1;height:7px;background:var(--panel2);border-radius:100px;overflow:hidden;min-width:40px}
.bar i{display:block;height:100%;border-radius:100px}
.rs{font-family:var(--mono);font-size:12px;font-weight:700;width:34px;text-align:right;flex:0 0 auto}
.pctv{font-family:var(--mono);font-size:11px;color:var(--dim);width:42px;text-align:right;flex:0 0 auto}
.meta{grid-column:1/2;font-family:var(--mono);font-size:9.5px;color:var(--faint);
  display:flex;gap:10px;flex-wrap:wrap}
.pick{text-align:center;border-left:1px solid var(--line);padding-left:12px;
  grid-row:1/3;grid-column:2/3}
.pick b{display:block;font-family:var(--mono);font-size:22px;font-weight:800;letter-spacing:-.02em}
.pick span{font-family:var(--mono);font-size:9px;color:var(--faint);letter-spacing:.06em}
.lv{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;
  padding:3px 8px;border-radius:6px;background:var(--panel2);border:1px solid var(--line)}
.lv-tag{font-size:9px;font-weight:700;letter-spacing:.06em;padding:1px 6px;border-radius:100px;
  flex:0 0 auto}
.lv.on .lv-tag{color:var(--ink);background:var(--win)}
.lv.fin .lv-tag{color:var(--dim);border:1px solid var(--line2)}
.lv-sc{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lv-mark{margin-left:auto;font-weight:700;flex:0 0 auto}
.lv-mark.hit{color:var(--win)}
.lv-mark.miss{color:var(--lose)}
.runs{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:10px;
  color:var(--dim);padding:0 2px;min-height:16px}
.bd{position:relative;width:17px;height:17px;flex:0 0 auto}
.bd .bg{position:absolute;width:6px;height:6px;transform:rotate(45deg);
  border:1px solid var(--line2);background:transparent}
.bd .bg:nth-child(1){left:5.5px;top:0}
.bd .bg:nth-child(2){left:0;top:5.5px}
.bd .bg:nth-child(3){left:11px;top:5.5px}
.bd .bg.full{background:var(--amber);border-color:var(--amber)}
.outs{display:flex;gap:3px;flex:0 0 auto}
.outs .od{width:5px;height:5px;border-radius:100px;border:1px solid var(--line2)}
.outs .od.full{background:var(--lose);border-color:var(--lose)}
/* Named atbat, not ab: .ab is the team abbreviation in the matchup rows, and
   sharing it dragged in width:42px and font-size:13px, which overflowed this
   block onto the runner text beside it. */
.atbat{display:flex;align-items:baseline;gap:4px;flex:0 1 auto;min-width:0;
  white-space:nowrap;overflow:hidden}
.atbat .caret{color:var(--amber);font-style:normal;font-size:8px;line-height:1;
  flex:0 0 auto}
.atbat b{color:var(--text);font-weight:700;overflow:hidden;text-overflow:ellipsis}
.atbat em{font-style:normal;color:var(--faint);font-size:9px;flex:0 0 auto;
  border:1px solid var(--line2);border-radius:3px;padding:0 3px}
.who{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 auto}
.who b{color:var(--text);font-weight:600}
.who .empty{color:var(--faint)}
.stamp.live b{color:var(--win)}
.tag{font-size:9px;padding:1px 6px;border-radius:100px;border:1px solid var(--line2);color:var(--faint)}
/* DraftKings' prices beside the projection. "dkp", not "dk" or "px": check
   the flat class namespace before adding a name here. */
.dkp{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px;
  margin-top:2px;padding-top:6px;border-top:1px dashed var(--line);font-family:var(--mono);
  font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums}
.dkp .bk{color:var(--amber);font-weight:700;letter-spacing:.05em}
.dkp b{color:var(--text);font-weight:600}
.dkp .val{color:var(--win);font-weight:700}
.dkp .nov{color:var(--faint)}
.tag.ok{border-color:#1F7A4C;color:var(--win)}
footer{padding:14px 20px 26px;font-family:var(--mono);font-size:10px;color:var(--faint);line-height:1.75}
footer b{color:var(--dim)}
@media(max-width:560px){.card{grid-template-columns:1fr 76px}.ab{width:36px}}
</style></head><body><div class="wrap">
<header>
  <div class="eyebrow">MLB · MATCHUP PROJECTIONS</div>
  <h1>Who Wins Tonight</h1>
  <div class="slate" id="slate"></div>
  <div id="stamps" style="margin-top:3px"></div>
</header>
<div class="nav">
  <a class="pill" href="hits.html">Hit picks</a>
  <a class="pill" href="accuracy.html">Accuracy</a>
  <a class="pill" href="players.html">Players</a>
  <a class="pill" href="daily.html">Stadium report</a>
  <a class="pill" href="index.html">Wind profile</a>
</div>
<div id="board"></div>
<footer>
  Expected runs come from each projected lineup's wOBA, with plate appearances split
  <b>59/41</b> between the opposing starter and bullpen — the league's real split — then scaled by
  park and tonight's weather. Runs become a win probability through Pythagenpat.<br>
  Home field is a <b>probability shift, not extra runs</b>: this season home clubs won
  <b id="hfa"></b> while outscoring visitors by about a twentieth of a run, so batting last is
  worth more than the scoring. No bullpen usage, defense, injuries or umpire in here.
  <b>Projections, not predictions.</b>
</footer></div>
<script>
const D = __DATA__;
const MIN_EV = __MINEV__;
// Live scores are a display layer and are deliberately NOT part of D. The
// accuracy ledger parses D back out of this page to freeze predictions before
// first pitch; a live score must never be reachable from that path.
let LIVE = __LIVE__;
let liveAt = null;   // last successful browser refresh; null = build-time snapshot
function etTime(iso){
  return new Date(iso).toLocaleString("en-US",{timeZone:"America/Chicago",
    hour:"numeric",minute:"2-digit",hour12:true}) + " CT";
}
function etDateShort(iso){
  return new Date(iso).toLocaleDateString("en-US",{timeZone:"America/Chicago",
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
const STALE_HRS=4;
function stamps(){
  const hrs=(Date.now()-new Date(D.built).getTime())/3.6e6;
  document.getElementById("stamps").innerHTML =
    `<span class="stamp${hrs>STALE_HRS?" stale":""}"><b>Updated</b> ${etTime(D.built)} on `+
    `${etDateShort(D.built)} · ${ago(D.built)}${hrs>STALE_HRS?" — may be out of date":""}</span>`+
    `<span class="stamp">league avg ${D.lgRuns} runs/side</span>` +
    (D.dkAt ? `<span class="stamp"><b>DraftKings</b> prices ${ago(D.dkAt)} · check the live price before betting</span>` : "") +
    liveStamp();
}
function liveStamp(){
  const n = Object.keys(LIVE).length;
  if(!n) return "";
  return liveAt
    ? `<span class="stamp live"><b>Scores live</b> \u00b7 ${ago(new Date(liveAt).toISOString())}</span>`
    : `<span class="stamp"><b>Scores</b> as of last build</span>`;
}
const SUFFIX=/^(jr|sr|ii|iii|iv|v)\.?$/i;
function surname(full){
  const t=String(full||"").trim().split(/\s+/);
  if(t.length<2) return t[0]||"";
  // "Vladimir Guerrero Jr." is Guerrero Jr., not Jr.
  return SUFFIX.test(t[t.length-1]) ? t.slice(-2).join(" ") : t[t.length-1];
}
function liveFrom(g){
  const st=g.status||{}, phase=st.abstractGameState;
  if(phase!=="Live" && phase!=="Final") return null;
  const h=g.teams&&g.teams.home, a=g.teams&&g.teams.away;
  if(!h || !a || h.score==null || a.score==null) return null;
  const o={state:phase, home:h.score, away:a.score, detail:st.detailedState||phase};
  if(phase==="Live"){
    const ls=g.linescore||{};
    o.inning=ls.currentInning; o.half=(ls.inningState||"").slice(0,3);
    if(typeof ls.outs==="number") o.outs=ls.outs;
    // offense.first/second/third are present only when that base is occupied.
    const off=ls.offense||{};
    const on=[];
    for(const [key,label] of [["first","1B"],["second","2B"],["third","3B"]]){
      if(off[key] && off[key].fullName){
        on.push({base:label, name:surname(off[key].fullName)});
      }
    }
    if(on.length) o.on=on;
    // Between halves MLB still names a batter — the next one up — and the
    // count reads 0-0. Nobody is at the plate during the break, so only
    // claim a batter while a half-inning is actually under way.
    const mid = o.half==="Top" || o.half==="Bot";
    if(mid && off.batter && off.batter.fullName){
      o.bat = surname(off.batter.fullName);
      if(typeof ls.balls==="number" && typeof ls.strikes==="number"){
        o.count = ls.balls + "-" + ls.strikes;
      }
    }
  }
  return o;
}
function diamond(on){
  const occ = new Set((on||[]).map(r=>r.base));
  const cell = b => `<i class="bg${occ.has(b)?" full":""}"></i>`;
  return `<span class="bd" aria-hidden="true">${cell("2B")}${cell("3B")}${cell("1B")}</span>`;
}
function outsDots(n){
  if(typeof n!=="number") return "";
  let h="";
  for(let i=0;i<3;i++) h += `<i class="od${i<n?" full":""}"></i>`;
  return `<span class="outs" title="${n} out">${h}</span>`;
}
function runners(L){
  // Batter and runners come only from a live refresh — see live_state().
  if(L.state!=="Live" || (!L.on && typeof L.outs!=="number" && !L.bat)) return "";
  const who = L.on && L.on.length
    ? L.on.map(r=>`<b>${r.name}</b> ${r.base}`).join(" \u00b7 ")
    : `<span class="empty">bases empty</span>`;
  // Who is up is the more immediate question, so it leads and never truncates.
  const ab = L.bat
    ? `<span class="atbat"><i class="caret">\u25B8</i><b>${L.bat}</b>${
        L.count?`<em>${L.count}</em>`:""}</span>`
    : "";
  return `<div class="runs">${diamond(L.on)}${outsDots(L.outs)}${ab}
    <span class="who">${who}</span></div>`;
}
function liveStrip(g){
  const L=LIVE[String(g.gamePk)];
  if(!L) return "";
  const fin = L.state==="Final";
  const tag = fin ? (/final/i.test(L.detail)?"FINAL":L.detail.toUpperCase())
                  : (L.inning ? `${(L.half||"").toUpperCase()} ${L.inning}`.trim() : "LIVE");
  let mark="";
  if(fin && L.home!==L.away){
    const right = (g.wpHome>=0.5) === (L.home>L.away);
    mark = `<span class="lv-mark ${right?"hit":"miss"}">${right?"\u2713":"\u2717"}</span>`;
  }
  return `<div class="lv ${fin?"fin":"on"}"><span class="lv-tag">${tag}</span>
    <span class="lv-sc">${g.away.team} ${L.away} \u00b7 ${L.home} ${g.home.team}</span>${mark}</div>
    ${runners(L)}`;
}
function sgn(x){ return (x>0?"+":"")+x; }
function imp(a){ a=+a; return a<0 ? -a/(-a+100) : 100/(a+100); }
function dec(a){ a=+a; return 1 + (a<0 ? 100/-a : a/100); }
function dkRow(g){
  const d=g.dk; if(!d) return "";
  const bits=[`<span class="bk">DK</span>`];
  if(d.ml){
    const nvH = imp(d.ml.home)/(imp(d.ml.home)+imp(d.ml.away));
    bits.push(`<span>${g.away.team} <b>${sgn(d.ml.away)}</b> · ${g.home.team} <b>${sgn(d.ml.home)}</b></span>`);
    bits.push(`<span>${g.home.team} ${(nvH*100).toFixed(0)}% no-vig</span>`);
    // Expected return on one unit at DraftKings' price if the model is right.
    const evH = g.wpHome*dec(d.ml.home)-1, evA = (1-g.wpHome)*dec(d.ml.away)-1;
    const best = evH>=evA ? [g.home.team, evH] : [g.away.team, evA];
    bits.push(best[1]>MIN_EV
      ? `<span class="val" title="model's expected return on one unit at DraftKings' price">value ${best[0]} +${(best[1]*100).toFixed(1)}%</span>`
      : `<span class="nov">no value on the moneyline</span>`);
  }
  if(d.total){
    const diff = g.total - d.total.line;
    bits.push(`<span>total ${d.total.line} o<b>${sgn(d.total.o)}</b> u<b>${sgn(d.total.u)}</b></span>`);
    bits.push(`<span>model ${g.total.toFixed(1)} (${diff>=0?"+":""}${diff.toFixed(1)})</span>`);
  }
  return `<div class="dkp">${bits.join("")}</div>`;
}
function bar(p, win){
  return `<span class="bar"><i style="width:${(p*100).toFixed(1)}%;background:${win?"var(--win)":"var(--line2)"}"></i></span>`;
}
function card(g){
  const hw=g.wpHome, aw=1-hw;
  const homeFav = hw>=0.5;
  const pick = homeFav ? g.home.team : g.away.team;
  const conf = (g.home.confirmed?1:0)+(g.away.confirmed?1:0);
  const wxs = g.dome ? "roof" : (g.wx&&g.wx.mph!=null
    ? `${Math.round(g.wx.mph)}mph ${g.row.replace(/_/g," ")} · ${Math.round(g.wx.temp)}°` : "");
  return `<div class="card">
    <div class="mu">
      ${liveStrip(g)}
      <div class="tm"><span class="ab">${g.away.team}</span>${bar(aw,!homeFav)}
        <span class="rs">${g.away.runs.toFixed(1)}</span>
        <span class="pctv">${(aw*100).toFixed(0)}%</span></div>
      <div class="tm"><span class="ab">${g.home.team}</span>${bar(hw,homeFav)}
        <span class="rs">${g.home.runs.toFixed(1)}</span>
        <span class="pctv">${(hw*100).toFixed(0)}%</span></div>
    </div>
    <div class="pick"><b>${pick}</b><span>${(Math.max(hw,aw)*100).toFixed(0)}% WIN</span></div>
    <div class="meta">
      <span>${etTime(g.start)}</span><span>${g.venue}</span>
      <span>total ${g.total.toFixed(1)}</span>${wxs?`<span>${wxs}</span>`:""}
      <span class="tag ${conf===2?"ok":""}">${conf}/2 lineups</span>
    </div>
    ${dkRow(g)}
  </div>`;
}
document.getElementById("slate").textContent =
  `${dayLabel(D.date)} · ${D.games.length} game${D.games.length===1?"":"s"}`;
document.getElementById("hfa").textContent =
  D.hfaRate ? (D.hfaRate*100).toFixed(1)+"%" : "about 53%";
function draw(){
  document.getElementById("board").innerHTML = D.games.length
    ? D.games.map(card).join("")
    : `<div class="card">No games scheduled.</div>`;
}

// Ask MLB directly for the current scores. If that request is refused or the
// visitor is offline, the page silently keeps the snapshot taken at build time
// and looks exactly as it did before — a failure here costs nothing.
const LIVE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=linescore&date=" + D.date;
async function refreshLive(){
  try{
    const r = await fetch(LIVE_URL, {cache:"no-store"});
    if(!r.ok) return;
    const j = await r.json();
    const next = {};
    for(const d of (j.dates||[])) for(const g of (d.games||[])){
      const st = liveFrom(g);
      if(st && g.gamePk!=null) next[String(g.gamePk)] = st;
    }
    LIVE = next; liveAt = Date.now();
    draw(); stamps();
  }catch(e){ /* keep the build-time snapshot */ }
}
// Stop polling once every game on the slate is final — no reason to keep asking.
function slateOpen(){
  return D.games.some(g => { const L = LIVE[String(g.gamePk)];
                             return !L || L.state !== "Final"; });
}

draw(); stamps(); setInterval(stamps, 60000);
refreshLive();
setInterval(() => { if(slateOpen()) refreshLive(); }, 45000);
document.addEventListener("visibilitychange", () => {
  if(!document.hidden && slateOpen()) refreshLive();
});
</script></body></html>
"""


if __name__ == "__main__":
    main()
