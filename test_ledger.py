#!/usr/bin/env python3
"""Tests for the ledger's honesty rules.

The accuracy page is only worth anything if the ledger cannot cheat, and the
three rules that stop it cheating were checked by hand until now. A result
that leaks onto a prediction before first pitch is not a cosmetic bug — it is
the page telling a lie about its own record — so the rules are pinned here.

    python3 test_ledger.py
"""
import json
import sys

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
