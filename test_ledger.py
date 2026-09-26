#!/usr/bin/env python3
"""Tests for the ledger's honesty rules.

The accuracy page is only worth anything if the ledger cannot cheat, and the
three rules that stop it cheating were checked by hand until now. A result
that leaks onto a prediction before first pitch is not a cosmetic bug — it is
the page telling a lie about its own record — so the rules are pinned here.

    python3 test_ledger.py
"""
import json
import re
import sys
from pathlib import Path

import dk
import players as P
import ledger as L

FAILED = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def stub(sched, boxes, seen=None):
    """Stand in for the MLB API so the rules can be tested without a network."""
    def q(path, **kw):
        if seen is not None:
            seen.append(path)
        if path == "schedule":
            return sched
        if path.startswith("game/"):
            return boxes[int(path.split("/")[1])]
        raise AssertionError(path)
    return q


def game(pk, state, home, away):
    return {"gamePk": pk, "status": {"codedGameState": state},
            "teams": {"home": {"score": home}, "away": {"score": away}}}


def box(batting):
    """batting: {pid: (plateAppearances, hits)}"""
    return {"teams": {
        "home": {"players": {f"ID{p}": {"person": {"id": p},
                                        "stats": {"batting": {"plateAppearances": pa,
                                                              "hits": h} if pa is not None else {}}}
                             for p, (pa, h) in batting.items()}},
        "away": {"players": {}}}}


def test_doubleheader_is_scored_per_game():
    """The regression this file exists for.

    Two games on one date, one player in both. The old scorer matched on date
    and player id alone, so it graded both games with the player's day total —
    and stamped game two's result on before game two had started, which also
    froze a prediction the lineup logic still needed to revise.
    """
    seen = []
    P.q = stub({"dates": [{"games": [game(1, "F", 5, 3), game(2, "I", 1, 0)]}]},
               {1: box({1: (4, 2), 2: (3, 0), 9: (None, None)})}, seen)
    led = {"hits": [{"date": "D", "gamePk": 1, "pid": 1, "p": .7, "result": None},
                    {"date": "D", "gamePk": 2, "pid": 1, "p": .7, "result": None},
                    {"date": "D", "gamePk": 1, "pid": 2, "p": .7, "result": None},
                    {"date": "D", "gamePk": 1, "pid": 9, "p": .7, "result": None}],
           "games": [{"date": "D", "gamePk": 1, "rHome": 4.0, "rAway": 4.0, "result": None},
                     {"date": "D", "gamePk": 2, "rHome": 4.0, "rAway": 4.0, "result": None}]}
    L.score(led)
    h = {(r["gamePk"], r["pid"]): r["result"] for r in led["hits"]}
    check(h[(1, 1)] is True, "a hit in the finished game scores True")
    check(h[(1, 2)] is False, "no hit in the finished game scores False")
    check(h[(1, 9)] is None, "a player who never batted stays unscored")
    check(h[(2, 1)] is None,
          "the same player's row in the game still in progress is NOT scored")
    check("game/2/boxscore" not in seen, "no box score is fetched for an unfinished game")
    g = {r["gamePk"]: r for r in led["games"]}
    check(g[1]["result"] is True and g[1]["sHome"] == 5 and g[1]["sAway"] == 3,
          "a finished game records both the winner and the final score")
    check(g[2]["result"] is None and g[2].get("sHome") is None,
          "an unfinished game records nothing at all")


def test_plate_appearances_are_recorded_and_backfilled():
    """PA is what separates the two explanations for the picks running hot.

    hits.py builds P(at least one hit) from a per-PA rate and a FIXED PA per
    lineup slot. Through 2026-09-20 the picks came in 3.7 points under their
    projection, and fitting a correction to the outcome alone could not say
    whether the rate or the PA count was to blame — a PA shrink, a rate
    shrink and a flat logit shrink all scored identically out of sample.
    Recording the actual PA settles it.
    """
    P.q = stub({"dates": [{"games": [game(1, "F", 5, 3)]}]},
               {1: {"teams": {"home": {"players": {
                       "ID1": {"person": {"id": 1}, "battingOrder": "300",
                               "stats": {"batting": {"plateAppearances": 5, "hits": 2}}},
                       "ID2": {"person": {"id": 2}, "battingOrder": "401",
                               "stats": {"batting": {"plateAppearances": 3, "hits": 0}}}}},
                     "away": {"players": {}}}}})
    led = {"hits": [
        # already settled: the PA must backfill without touching the result
        {"date": "D", "gamePk": 1, "pid": 1, "slot": 3, "p": .7, "result": True},
        # unsettled: scores and records PA in one pass
        {"date": "D", "gamePk": 1, "pid": 2, "slot": 4, "p": .7, "result": None}],
        "games": []}
    L.score(led)
    a, b = led["hits"]
    check(a["pa"] == 5 and a["h"] == 2, "PA backfills onto an already-settled row")
    check(a["result"] is True, "backfilling PA does not disturb the settled result")
    check(b["result"] is False and b["pa"] == 3, "an open row gets both at once")
    check(a["slotActual"] == 3, "the actual batting slot is recorded")
    check(b["slotActual"] == 4, "a substitute's slot reads from the hundreds digit")
    snap = json.dumps(led, sort_keys=True)
    L.score(led)
    check(json.dumps(led, sort_keys=True) == snap, "re-scoring changes nothing")

    summary = L.summarise({"hits": led["hits"], "games": []})["hits"]["pa"]
    check(summary["n"] == 2, "the PA summary counts the rows that have one")
    # slot 3 assumes 4.43 and got 5; slot 4 assumes 4.32 and got 3.
    check(abs(summary["gap"] - ((4.43 - 5) + (4.32 - 3)) / 2) < 1e-9,
          "the summary reports assumed minus actual PA")


def test_scoring_is_idempotent_and_backfills():
    P.q = stub({"dates": [{"games": [game(1, "F", 5, 3)]}]}, {1: box({1: (4, 2)})})
    led = {"hits": [], "games": [{"date": "D", "gamePk": 1, "rHome": 4.0,
                                  "rAway": 4.0, "result": True}]}
    L.score(led)
    check(led["games"][0]["sHome"] == 5 and led["games"][0]["sAway"] == 3,
          "a final score backfills onto a row that already has its outcome")
    snap = json.dumps(led, sort_keys=True)
    L.score(led)
    L.score(led)
    check(json.dumps(led, sort_keys=True) == snap, "re-scoring is a no-op")


def test_settled_rows_are_never_revised():
    P.q = stub({"dates": [{"games": [game(1, "F", 5, 3)]}]}, {1: box({1: (4, 2)})})
    led = {"hits": [{"date": "D", "gamePk": 1, "pid": 1, "p": .7, "result": False}],
           "games": [{"date": "D", "gamePk": 1, "rHome": 4.0, "rAway": 4.0,
                      "result": False, "sHome": 9, "sAway": 9}]}
    L.score(led)
    check(led["hits"][0]["result"] is False, "a settled hit row is left alone")
    check(led["games"][0]["result"] is False and led["games"][0]["sHome"] == 9,
          "a settled game row is left alone")


def test_fetch_failure_writes_nothing():
    def boom(path, **kw):
        raise OSError("no network")
    P.q = boom
    led = {"hits": [{"date": "D", "gamePk": 1, "pid": 1, "p": .7, "result": None}],
           "games": [{"date": "D", "gamePk": 1, "rHome": 4.0, "rAway": 4.0, "result": None}]}
    L.score(led)
    check(led["hits"][0]["result"] is None and led["games"][0]["result"] is None,
          "a fetch failure scores nothing rather than guessing")


def test_record_refuses_a_game_already_under_way():
    """Rule 1, which was previously only checked by hand."""
    import tempfile
    from pathlib import Path
    past, future = "2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"

    def hits_page(start):
        return ('<script>const D = ' + json.dumps({
            "date": "D",
            "games": [{"gamePk": 1, "start": start,
                       "picks": [{"id": 1, "name": "A", "team": "T", "slot": 1, "p": .7}]}]
        }) + ';\n</script>')

    def games_page(start):
        return ('<script>const D = ' + json.dumps({
            "date": "D",
            "games": [{"gamePk": 1, "start": start, "wpHome": .6,
                       "home": {"team": "H", "runs": 4.5},
                       "away": {"team": "A", "runs": 4.0}}]
        }) + ';\n</script>')

    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        for tag, start in (("s", past), ("u", future)):
            (d / f"h{tag}.html").write_text(hits_page(start))
            (d / f"g{tag}.html").write_text(games_page(start))
        led = L.record({"hits": [], "games": []}, str(d / "hs.html"), str(d / "gs.html"))
        check(led["hits"] == [] and led["games"] == [],
              "nothing is recorded for a game that has started")
        led = L.record({"hits": [], "games": []}, str(d / "hu.html"), str(d / "gu.html"))
        check(len(led["hits"]) == 1 and len(led["games"]) == 1,
              "a game still ahead of us is recorded")
        check(led["hits"][0]["result"] is None and led["games"][0]["result"] is None,
              "a freshly recorded row carries no result")


def test_live_scores_cannot_reach_the_ledger():
    """The games page carries live scores; the ledger must not see them.

    games.py renders the current score into its own `LIVE` constant, separate
    from the `D` payload the ledger parses. If the two were ever merged, a
    result could reach a prediction that has not been frozen yet — the exact
    failure the ledger exists to prevent. This pins them apart.
    """
    import tempfile
    from pathlib import Path
    future = "2099-01-01T00:00:00Z"
    page = ('<script>\nconst D = ' + json.dumps({
        "date": "D",
        "games": [{"gamePk": 1, "start": future, "wpHome": .6,
                   "home": {"team": "H", "runs": 4.5},
                   "away": {"team": "A", "runs": 4.0}}]
    }) + ';\n'
        + 'let LIVE = ' + json.dumps({"1": {"state": "Live", "home": 7, "away": 2}})
        + ';\n</script>')

    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "games.html"
        f.write_text(page)
        led = L.record({"hits": [], "games": []}, str(Path(t) / "none.html"), str(f))
        check(len(led["games"]) == 1, "the page still records its prediction")
        row = led["games"][0]
        check(row["rHome"] == 4.5 and row["rAway"] == 4.0,
              "the recorded runs are the projection, not the live score")
        check(row["result"] is None and row.get("sHome") is None,
              "a live score does NOT settle the row before the game is over")
        check(7 not in (row.get("sHome"), row.get("rHome")),
              "the live home score never appears anywhere on the row")


def bare_class_rules(css):
    """Class names defined by a standalone `.name{...}` rule, outside @media.

    Only bare selectors count: `.bd .bg`, `.tag.ok` and `.lv.on .lv-tag` are
    scoped to a component and cannot collide. `@media` blocks are skipped
    because a responsive override of the same class is the point.
    """
    depth, out, i = 0, [], 0
    top = []
    # Strip @media blocks by brace matching.
    while i < len(css):
        if css.startswith("@media", i):
            j, d = css.index("{", i), 0
            while j < len(css):
                if css[j] == "{":
                    d += 1
                elif css[j] == "}":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            i = j + 1
            continue
        top.append(css[i])
        i += 1
    top = "".join(top)
    for block in top.split("}"):
        if "{" not in block:
            continue
        for sel in block.split("{")[0].split(","):
            sel = sel.strip()
            if re.fullmatch(r"\.[A-Za-z][-\w]*", sel):
                out.append(sel[1:])
    return out


def test_no_two_components_share_a_class_name():
    """Two components sharing a class name is a silent layout bug.

    This bit us for real: the at-bat block was given `class="ab"`, which was
    already the team abbreviation in the matchup rows. It inherited that rule's
    `width:42px` and `font-size:13px`, so the batter's name rendered oversized
    and overflowed its fixed box onto the runner text beside it. Nothing
    errored — the page just looked wrong, and only on games that were live.
    """
    from collections import Counter
    for name in ("games.py", "ledger.py"):
        src = (Path(__file__).parent / name).read_text()
        if "<style>" not in src:
            continue
        css = src[src.index("<style>"):src.index("</style>")]
        dupes = sorted(c for c, n in Counter(bare_class_rules(css)).items() if n > 1)
        check(not dupes,
              f"{name}: no class is defined twice at the top level"
              + (f" (found: {', '.join('.' + d for d in dupes)})" if dupes else ""))


def test_build_snapshot_carries_no_baserunners():
    """The build-time score snapshot must not include who is on base.

    games.py freezes a snapshot so the page still shows scores when the
    visitor's browser cannot reach MLB. That snapshot can be hours old by the
    time anyone opens the page. A stale score is merely old; a stale runner on
    second is a false claim about the state of the game. Runners are therefore
    shown only from a live browser refresh, and this pins live_state() to emit
    none — the property is invisible until someone reads a frozen page.
    """
    src = (Path(__file__).parent / "games.py").read_text()
    ns = {}
    exec(src[src.index("def live_state(g):"):src.index("def pitch_mult(")], ns)
    snap = ns["live_state"]({
        "gamePk": 1,
        "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
        "teams": {"home": {"score": 3}, "away": {"score": 2}},
        "linescore": {"currentInning": 7, "inningState": "Bottom", "outs": 2,
                      "balls": 2, "strikes": 1,
                      "offense": {"batter": {"fullName": "C Hitter"},
                                  "first": {"fullName": "A Batter"},
                                  "third": {"fullName": "B Runner"}}}})
    check(snap["home"] == 3 and snap["away"] == 2, "the snapshot carries the score")
    check(snap["inning"] == 7, "the snapshot carries the inning")
    check("on" not in snap, "the snapshot carries NO baserunners")
    check("outs" not in snap, "the snapshot carries NO out count")
    check("bat" not in snap, "the snapshot carries NO batter")
    check("count" not in snap, "the snapshot carries NO ball-strike count")
    blob = json.dumps(snap)
    check(all(n not in blob for n in ("Batter", "Runner", "Hitter")),
          "no player name appears anywhere in the snapshot")


def test_summarise_reports_run_bias():
    led = {"hits": [], "games": [
        {"date": "D", "gamePk": 1, "home": "H", "away": "A", "wpHome": .6,
         "rHome": 5.0, "rAway": 4.0, "result": True, "sHome": 4, "sAway": 3},
        {"date": "D", "gamePk": 2, "home": "H", "away": "A", "wpHome": .6,
         "rHome": 5.0, "rAway": 4.0, "result": True, "sHome": 6, "sAway": 3}]}
    r = L.summarise(led)["runs"]
    check(r["n"] == 2, "run accuracy counts the games with a final score")
    # projected 9.0 both games; actual 7 and 9, so the model ran 1.0 heavy.
    check(abs(r["total"]["bias"] - 1.0) < 1e-9, "total bias is projected minus actual")
    check(abs(r["total"]["mae"] - 1.0) < 1e-9, "total mean absolute error is right")
    check(abs(r["home"]["mae"] - 1.0) < 1e-9, "home mean absolute error is right")
    # projected margin +1.0; actual +1 and +3, so the model was 1.0 light on it.
    check(abs(r["margin"]["bias"] + 1.0) < 1e-9, "margin bias is right")
    check(abs(r["predTotal"] - 9.0) < 1e-9 and abs(r["actTotal"] - 8.0) < 1e-9,
          "mean projected and actual totals are right")


def test_summarise_omits_runs_when_no_scores():
    led = {"hits": [], "games": [{"date": "D", "gamePk": 1, "home": "H", "away": "A",
                                  "wpHome": .6, "rHome": 5.0, "rAway": 4.0, "result": True}]}
    check("runs" not in L.summarise(led),
          "no run card is claimed before any final score is known")


# ── DraftKings ──────────────────────────────────────────────────────────
def _odd(stat, side, price, line=None, bet="ou", ent="all", pid=None, period="game"):
    o = {"statID": stat, "sideID": side, "betTypeID": bet, "statEntityID": ent,
         "periodID": period, "byBookmaker": {"draftkings": {"odds": price, "available": True}}}
    if line is not None:
        o["byBookmaker"]["draftkings"]["overUnder"] = line
    if pid:
        o["playerID"] = pid
    return o


def _event(eid="e1", start="2026-09-23T23:10:00.000Z", started=False,
           home="ARIZONA_DIAMONDBACKS_MLB", away="WASHINGTON_NATIONALS_MLB"):
    """Shaped like a real SportsGameOdds MLB event (September 2026)."""
    odds = [
        _odd("points", "home", "-165", bet="ml", ent="home"),
        _odd("points", "away", "+144", bet="ml", ent="away"),
        _odd("points", "over", "-110", "8.5"), _odd("points", "under", "-110", "8.5"),
        # First five innings: a different bet.
        _odd("points", "home", "-150", bet="ml", ent="home", period="1h"),
        _odd("batting_hits", "over", "-250", "0.5", pid="P1"),
        _odd("batting_hits", "under", "+190", "0.5", pid="P1"),
        _odd("batting_hits", "yes", "-240", bet="yn", ent="P1", pid="P1"),
        _odd("batting_hits", "no", "+185", bet="yn", ent="P1", pid="P1"),
        # Only the yes/no form for this hitter.
        _odd("batting_hits", "yes", "-150", bet="yn", ent="P2", pid="P2"),
        _odd("batting_hits", "no", "+120", bet="yn", ent="P2", pid="P2"),
        # 2+ hits is not 1+ hit.
        _odd("batting_hits", "over", "+160", "1.5", pid="P3"),
        _odd("batting_hits", "under", "-210", "1.5", pid="P3"),
    ]
    return {"eventID": eid, "status": {"startsAt": start, "started": started},
            "teams": {"home": {"teamID": home}, "away": {"teamID": away}},
            "players": {"P1": {"name": "Ronald Acuña Jr."}, "P2": {"name": "CJ Abrams"},
                        "P3": {"name": "Luis Arraez"}},
            "odds": {str(i): o for i, o in enumerate(odds)}}


def test_dk_reads_the_three_markets():
    e = dk.parse(_event())
    check(e["ml"] == {"home": -165, "away": 144}, "the full-game moneyline is read, not the first five")
    check(e["total"] == {"line": 8.5, "o": -110, "u": -110}, "the game total is read with its line")
    check(e["hits"].get("ronaldacuna") == {"o": -250, "u": 190},
          "1+ hit prefers the over/under and folds the accent in the name")
    check(e["hits"].get("cjabrams") == {"o": -150, "u": 120}, "a yes/no alone is 1+ hit")
    check("luisarraez" not in e["hits"], "o1.5 hits is not mistaken for 1+ hit")


def test_dk_matches_the_right_game():
    cache = {"events": [dk.parse(_event("g1", "2026-09-23T17:10:00Z")),
                        dk.parse(_event("g2", "2026-09-23T23:10:00Z"))]}
    dbacks = {"teamName": "D-backs", "name": "Arizona Diamondbacks"}
    nats = {"teamName": "Nationals", "name": "Washington Nationals"}
    ev = dk.find_game(cache, dbacks, nats, "2026-09-23T23:05:00Z")
    check(ev is not None and ev["id"] == "g2",
          "MLB's 'D-backs' still finds Arizona, and a doubleheader goes by start time")
    check(dk.find_game(cache, nats, dbacks, "2026-09-23T23:05:00Z") is None,
          "home and away are not interchangeable")
    check(dk.find_game(cache, dbacks, nats, "2026-09-25T23:05:00Z") is None,
          "a game days away is not matched to today's price")
    red = {"teamName": "Red Sox", "name": "Boston Red Sox"}
    white = {"teamName": "White Sox", "name": "Chicago White Sox"}
    sox = {"events": [dk.parse(_event("s", home="CHICAGO_WHITE_SOX_MLB",
                                      away="NEW_YORK_YANKEES_MLB"))]}
    check(dk.find_game(sox, red, {"teamName": "Yankees"}, "2026-09-23T23:10:00Z") is None,
          "the Red Sox are not the White Sox")


def test_dk_cache_spends_only_when_due():
    import tempfile
    from datetime import datetime, timezone, timedelta
    calls = []

    def get(path, params, key):
        calls.append(params)
        return {"data": [_event("a", "2026-09-23T23:10:00Z"),
                         _event("b", "2026-09-23T15:00:00Z", started=True)]}
    saved = dk.CACHE
    with tempfile.TemporaryDirectory() as tmp:
        dk.CACHE = Path(tmp) / "dk.json"
        try:
            t0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
            c = dk.load("2026-09-23", key="k", now=t0, get=get)
            check(len(calls) == 1 and [e["id"] for e in c["events"]] == ["a"],
                  "a refresh asks once, and a game already started is not priced")
            check(calls[0]["startsBefore"] == "2026-09-24T11:00:00Z",
                  "the window ends at 6am Central after the slate, so tomorrow costs nothing")
            dk.load("2026-09-23", key="k", now=t0 + timedelta(hours=3), get=get)
            check(len(calls) == 1, "a second build inside the refresh interval spends nothing")
            dk.load("2026-09-23", key="", now=t0 + timedelta(hours=5), get=get)
            check(len(calls) == 1, "no key, no request")

            def gone(path, params, key):
                calls.append(params)
                return {"data": []}
            c = dk.load("2026-09-23", key="k", now=datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
                        get=gone)
            check(len(calls) == 2 and [e["id"] for e in c["events"]] == ["a"],
                  "a game that has since started keeps its last pre-game price")

            def boom(path, params, key):
                raise OSError("down")
            c = dk.load("2026-09-23", key="k", now=datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc),
                        get=boom)
            check([e["id"] for e in c["events"]] == ["a"], "a failed refresh keeps the cached prices")
            c = dk.load("2026-09-24", key="", now=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
                        get=get)
            check(c["events"] == [], "yesterday's prices are never shown on today's slate")
        finally:
            dk.CACHE = saved


def test_dk_price_is_frozen_with_the_prediction():
    """A price is recorded with the pick, refreshed with it before first pitch,
    kept when a later build has none, and never touched once the game starts."""
    import tempfile
    future, past = "2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z"

    def pages(d, start, hdk, gdk):
        pick = {"id": 1, "name": "A", "team": "T", "slot": 1, "p": .7}
        if hdk:
            pick["dk"] = hdk
        g = {"gamePk": 1, "start": start, "wpHome": .6,
             "home": {"team": "H", "runs": 4.5}, "away": {"team": "A", "runs": 4.0}}
        if gdk:
            g["dk"] = gdk
        (d / "h.html").write_text('<script>const D = ' + json.dumps(
            {"date": "D", "games": [{"gamePk": 1, "start": start, "picks": [pick]}]}) + ';\n</script>')
        (d / "g.html").write_text('<script>const D = ' + json.dumps(
            {"date": "D", "games": [g]}) + ';\n</script>')
        return str(d / "h.html"), str(d / "g.html")

    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        g1 = {"ml": {"home": -150, "away": 130}, "total": None}
        led = L.record({"hits": [], "games": []}, *pages(d, future, {"o": -250, "u": 190}, g1))
        check(led["hits"][0].get("dk") == {"o": -250, "u": 190}
              and led["games"][0].get("dk") == g1, "a DraftKings price is recorded with the pick")
        led = L.record(led, *pages(d, future, None, None))
        check(led["hits"][0].get("dk") == {"o": -250, "u": 190},
              "a later build with no price keeps the recorded one")
        led = L.record(led, *pages(d, future, {"o": -270, "u": 205}, None))
        check(led["hits"][0]["dk"]["o"] == -270, "before first pitch the price follows the market")
        led = L.record(led, *pages(d, past, {"o": -400, "u": 300},
                                   {"ml": {"home": -300, "away": 250}}))
        check(led["hits"][0]["dk"]["o"] == -270 and led["games"][0]["dk"] == g1,
              "after first pitch nothing on the row moves, the price included")


def test_versus_dk_arithmetic():
    rows = [
        # Model 60% at +120 (decimal 2.2): EV +0.32, bet home, home wins: +1.2.
        {"wpHome": .6, "result": True, "dk": {"ml": {"home": 120, "away": -140}}},
        # Model 30% home at -200: away 70% at +170 is EV +0.89, bet away, home wins: -1.
        {"wpHome": .3, "result": True, "dk": {"ml": {"home": -200, "away": 170}}},
        # No price: not counted.
        {"wpHome": .5, "result": False},
    ]
    v = L.versus_dk(rows, lambda r: r["wpHome"],
                    lambda r: r.get("dk") and (r["dk"]["ml"]["home"], r["dk"]["ml"]["away"]))
    check(v["n"] == 2 and v["bets"] == 2 and v["won"] == 1, "value bets are counted on priced rows only")
    check(abs(v["profit"] - 0.2) < 1e-9, "flat-stake profit is +1.2 - 1 = +0.2 units")
    import math
    q1 = dk.no_vig(120, -140)
    check(abs(v["book"] - (-(math.log(q1) + math.log(dk.no_vig(-200, 170))) / 2)) < 1e-12,
          "DraftKings is scored on its no-vig chance")
    tot = L.totals_vs_dk([
        # Model 9.5 on a 7.5 line: over at -110; 10 runs scored, won 100/110.
        {"rHome": 5.0, "rAway": 4.5, "sHome": 6, "sAway": 4, "dk": {"total": {"line": 7.5, "o": -110, "u": -110}}},
        # Model 7.0 on 8.5: under at +100; 12 scored, lost.
        {"rHome": 3.5, "rAway": 3.5, "sHome": 9, "sAway": 3, "dk": {"total": {"line": 8.5, "o": -120, "u": 100}}},
        # Model 9.0 on 8.0, landed exactly 8: a push.
        {"rHome": 4.5, "rAway": 4.5, "sHome": 5, "sAway": 3, "dk": {"total": {"line": 8.0, "o": -110, "u": -110}}},
        # Not final yet: not graded.
        {"rHome": 4.5, "rAway": 4.5, "dk": {"total": {"line": 8.0, "o": -110, "u": -110}}},
    ])
    check(tot["n"] == 3 and tot["won"] == 1 and tot["push"] == 1 and tot["over"] == 2,
          "totals are graded on the side the projection pointed at, pushes apart")
    check(abs(tot["profit"] - (100 / 110 - 1)) < 1e-9, "a push neither wins nor loses")
    s = L.summarise({"hits": [], "games": [dict(r, date="D", gamePk=i, home="H", away="A",
                                                 rHome=4.0, rAway=4.0)
                                            for i, r in enumerate(rows)]})
    check(s.get("dk", {}).get("games", {}).get("n") == 2, "the accuracy page carries the comparison")


def test_value_list_ranks_on_the_shaded_model():
    c = [{"name": "A", "p": .72, "dk": {"o": -250, "u": 190}},   # 71.4% break-even
         {"name": "B", "p": .70, "dk": {"o": -150, "u": 120}},   # 60% break-even
         {"name": "C", "p": .66, "dk": {"o": -160, "u": 125}},
         {"name": "D", "p": .80}]                                  # no price
    v = dk.best_value(c, gap=0.0)
    check([x["name"] for x in v] == ["B", "C"],
          "ranked by expected return at DK's price; thin edges and unpriced hitters left out")
    check(abs(v[0]["ev"] - (0.70 * (1 + 100 / 150) - 1)) < 1e-4, "expected return is p x decimal - 1")
    v = dk.best_value(c, gap=0.05)
    check([x["name"] for x in v] == ["B"] and abs(v[0]["pAdj"] - 0.65) < 1e-9,
          "the model is shaded by its measured gap before it is compared")
    many = [{"name": str(i), "p": .75, "dk": {"o": -120 + i, "u": 100}} for i in range(15)]
    check(len(dk.best_value(many)) == 10, "the list stops at ten")


def test_calibration_gap_is_measured_not_assumed():
    rows = [{"p": .7, "result": i % 10 < 6} for i in range(300)] + [{"p": .7, "result": None}]
    gap, n = L.calibration({"hits": rows})
    check(n == 300 and abs(gap - 0.1) < 1e-9, "gap is mean predicted minus actual over settled picks")
    check(L.calibration({"hits": rows[:50]}) == (0.0, 50), "too few settled picks means no shading")


def test_value_picks_follow_the_ledger_rules():
    import tempfile
    future, past = "2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z"

    def page(d, vals):
        (d / "h.html").write_text('<script>const D = ' + json.dumps(
            {"date": "D", "games": [], "value": vals}) + ';\n</script>')
        return str(d / "h.html")

    def v(pid, start, gp=1, o=-150):
        return {"gamePk": gp, "id": pid, "name": f"P{pid}", "team": "T", "start": start,
                "p": .7, "pAdj": .67, "dk": {"o": o, "u": 120}, "ev": .1}

    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        none = str(d / "none.html")
        led = L.record({"hits": [], "games": []}, page(d, [v(1, future), v(2, future, 2), v(3, past, 3)]), none)
        check(sorted(r["pid"] for r in led["value"]) == [1, 2],
              "value picks are recorded before first pitch and never after")
        led = L.record(led, page(d, [v(2, future, 2, o=-170)]), none)
        check([r["pid"] for r in led["value"]] == [2] and led["value"][0]["dk"]["o"] == -170,
              "a pick that falls off the list before its game is dropped; one still on it follows the price")
        led["value"].append(dict(v(9, past, 9), date="D", pid=9, result=None))
        led = L.record(led, page(d, [v(2, future, 2, o=-170)]), none)
        check(9 in [r["pid"] for r in led["value"]], "a pick whose game has started is frozen, even off the list")

        sched = {"dates": [{"games": [game(2, "F", 5, 3), game(9, "F", 1, 0)]}]}
        boxes = {2: box({2: (4, 1)}), 9: box({77: (4, 0)})}
        saved = P.q
        P.q = stub(sched, boxes)
        try:
            L.score(led)
        finally:
            P.q = saved
        byp = {r["pid"]: r for r in led["value"]}
        check(byp[2]["result"] is True, "a value pick is settled from that game's box score")
        check(byp[9].get("void") and byp[9]["result"] is None,
              "a value pick who never batted is a void, not a loss")
        s = L.summarise(led)["dk"]["value"]
        check(s["n"] == 1 and s["won"] == 1 and s["void"] == 1 and abs(s["profit"] - 100 / 170) < 1e-9,
              "the accuracy page grades the list at DK's price and leaves voids out")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        sys.exit(1)
    print("all passed")
