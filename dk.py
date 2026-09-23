#!/usr/bin/env python3
"""
DraftKings prices for today's slate, through SportsGameOdds.

The model pages say what a game or a hitter is worth; this says what
DraftKings is asking, so the two can sit side by side and the ledger can keep
score of both. Three markets are read, because they are the three the model
has an answer for:

    moneyline        against games.py's win probability
    game total       against games.py's projected runs (a number, not a price:
                     the model has no run distribution to price an over with)
    1+ hit (o0.5)    against hits.py's P(at least one hit)

**The allowance is the constraint.** SportsGameOdds' free plan is 2,500
objects a month, counted about one per game returned, and the same key serves
mlb-streaks and nfl-streaks. This repo builds seven times a day; fetching on
every build would be ~100 games a day on its own. So the prices are cached in
`.cache/dk_odds.json` -- the Actions cache this workflow already restores for
Statcast -- and refreshed only when older than REFRESH_HOURS. At the Central
build times that is the 7am, 11am, 4pm and 10pm passes, and each one asks only
for games that have not started, so a full regular-season day costs ~40.

Only pre-game prices are ever fetched (`startsAfter` is now), so nothing here
can carry a result, and the ledger's first-pitch freeze covers the rest.

No key, no network, an odd response: the pages build without prices.

    python3 dk.py                  # refresh if due and print what is cached
"""

import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:  # noqa: BLE001
    CT = timezone(timedelta(hours=-5))

try:
    import certifi
    import ssl
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    import ssl
    SSL_CTX = ssl.create_default_context()

SGO = "https://api.sportsgameodds.com/v2"
BOOK = "draftkings"
CACHE = Path(__file__).parent / ".cache" / "dk_odds.json"
REFRESH_HOURS = 4
MAX_AGE_HOURS = 12      # older than this and a price is not shown at all
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# The smallest expected return the pages call "value", and the ledger bets.
# One threshold for both, so the record grades exactly what the pages flagged.
MIN_EV = 0.01


# ── prices ────────────────────────────────────────────────────────────────
def implied(american):
    a = float(american)
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def decimal(american):
    a = float(american)
    return 1 + (100 / -a if a < 0 else a / 100)


def no_vig(a, b):
    """The chance DraftKings' two prices imply for the first side, with the
    margin taken out proportionally."""
    x, y = implied(a), implied(b)
    return x / (x + y)


def hold(a, b):
    """The book's margin on a two-way market: implied chances over 100%."""
    return implied(a) + implied(b) - 1


def ev(p, american):
    """Expected return on one unit at this price if the true chance is p."""
    return p * decimal(american) - 1


# ── names ─────────────────────────────────────────────────────────────────
def norm(s):
    """Letters only, accents folded, lower case: "Acuña" and "Acuna" agree."""
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def norm_name(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    words = re.sub(r"[^a-z\s]", "", s.replace("-", " ").replace(".", " ")).split()
    return "".join(w for w in words if w not in SUFFIXES)


# ── the feed ──────────────────────────────────────────────────────────────
def _get(path, params, key):
    url = f"{SGO}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "X-Api-Key": key, "Accept": "application/json", "User-Agent": "mlb-wind/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def window(slate, now):
    """From now to 6am Central the morning after the slate: every game on
    today's card that has not started, and none of tomorrow's."""
    end = datetime.combine(slate + timedelta(days=1), time(6), CT).astimezone(timezone.utc)
    return now, end


def _price(o):
    dk = (o.get("byBookmaker") or {}).get(BOOK) or {}
    if not dk.get("available") or dk.get("odds") in (None, ""):
        return None, None
    try:
        price = int(str(dk["odds"]).replace("+", ""))
    except (TypeError, ValueError):
        return None, None
    line = dk.get("overUnder")
    try:
        line = float(line) if line not in (None, "") else None
    except (TypeError, ValueError):
        line = None
    return price, line


def parse(ev_):
    """One SportsGameOdds event -> the three markets, DraftKings only."""
    status = ev_.get("status") or {}
    teams = ev_.get("teams") or {}

    def label(t):
        t = t or {}
        n = t.get("names") or {}
        return " ".join(str(x) for x in (t.get("teamID"), n.get("long"), n.get("medium"),
                                          n.get("short")) if x)

    out = {"id": ev_.get("eventID"), "start": status.get("startsAt") or "",
           "home": label(teams.get("home")), "away": label(teams.get("away")),
           "ml": None, "total": None, "hits": {}}
    odds = ev_.get("odds") or {}
    ml = {}
    tot = {}
    hits = {}
    for o in odds.values():
        if o.get("periodID") != "game":
            continue
        price, line = _price(o)
        if price is None:
            continue
        stat, bet, side = o.get("statID"), o.get("betTypeID"), o.get("sideID")
        if stat == "points" and bet == "ml" and side in ("home", "away") \
                and o.get("statEntityID") == side:
            ml[side] = price
        elif stat == "points" and bet == "ou" and o.get("statEntityID") == "all" \
                and side in ("over", "under") and line is not None:
            tot[side] = (line, price)
        elif stat == "batting_hits" and o.get("playerID"):
            # 1+ hit comes as an over/under at 0.5 or as a yes/no; both are the
            # same bet. Anything else (o1.5) is a different bet and not ours.
            if bet == "ou" and line == 0.5 and side in ("over", "under"):
                hits.setdefault(o["playerID"], {}).setdefault("ou", {})[side[0]] = price
            elif bet == "yn" and side in ("yes", "no"):
                hits.setdefault(o["playerID"], {}).setdefault("yn", {})[
                    "o" if side == "yes" else "u"] = price
    if "home" in ml and "away" in ml:
        out["ml"] = ml
    if "over" in tot and "under" in tot and tot["over"][0] == tot["under"][0]:
        out["total"] = {"line": tot["over"][0], "o": tot["over"][1], "u": tot["under"][1]}
    players = ev_.get("players") or {}
    seen = {}
    for pid, forms in hits.items():
        pair = next((f for f in (forms.get("ou"), forms.get("yn"))
                     if f and "o" in f and "u" in f), None)
        name = (players.get(pid) or {}).get("name")
        if not pair or not name:
            continue
        k = norm_name(name)
        seen[k] = None if k in seen else pair     # one name, two hitters: skip both
    out["hits"] = {k: v for k, v in seen.items() if v}
    return out


def fetch(key, slate, now, get=_get):
    lo, hi = window(slate, now)
    params = {"leagueID": "MLB", "oddsAvailable": "true", "limit": 30,
              "startsAfter": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "startsBefore": hi.strftime("%Y-%m-%dT%H:%M:%SZ")}
    j = get("/events/", params, key)
    evs = [e for e in (j.get("data") or [])
           if not (e.get("status") or {}).get("started")
           and not (e.get("status") or {}).get("cancelled")]
    return [parse(e) for e in evs]


# ── the cache ─────────────────────────────────────────────────────────────
def _read():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None


def _age_hours(iso, now):
    try:
        return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600
    except (AttributeError, ValueError):
        return float("inf")


def load(slate, key=None, now=None, get=_get, write=True):
    """Today's cached prices, refreshed first if they are due and a key is set.

    A refresh replaces the prices of games still ahead and keeps the last
    pre-game prices of games that have since started, so a game in progress
    still shows what DraftKings was asking before first pitch.
    """
    now = now or datetime.now(timezone.utc)
    slate_s = slate.isoformat() if isinstance(slate, date) else str(slate)
    key = os.environ.get("SGO_API_KEY", "").strip() if key is None else key
    cache = _read()
    if not cache or cache.get("date") != slate_s:
        cache = {"date": slate_s, "at": None, "events": []}
    due = _age_hours(cache.get("at") or "", now) >= REFRESH_HOURS
    if key and due:
        try:
            fresh = fetch(key, date.fromisoformat(slate_s), now, get)
        except Exception as e:  # noqa: BLE001 - prices are an extra, not the page
            print(f"  dk: refresh failed ({type(e).__name__}); keeping the cached prices")
        else:
            keep = [e for e in cache["events"]
                    if e["id"] not in {f["id"] for f in fresh}
                    and _age_hours(e.get("start") or "", now) >= 0]
            cache = {"date": slate_s, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "events": keep + fresh}
            if write:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache, separators=(",", ":")))
            print(f"  dk: refreshed {len(fresh)} games from DraftKings "
                  f"({summary(fresh)})")
    elif not key:
        print("  dk: no SGO_API_KEY; building without DraftKings prices")
    if _age_hours(cache.get("at") or "", now) > MAX_AGE_HOURS:
        return {"date": slate_s, "at": None, "events": []}
    return cache


def summary(events):
    """Median DraftKings margin on each market, for the build log."""
    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None
    ml = med([hold(e["ml"]["home"], e["ml"]["away"]) for e in events if e.get("ml")])
    tot = med([hold(e["total"]["o"], e["total"]["u"]) for e in events if e.get("total")])
    hit = med([hold(h["o"], h["u"]) for e in events for h in e.get("hits", {}).values()])
    nh = sum(len(e.get("hits", {})) for e in events)
    part = [f"{nh} hit props"]
    for label, v in (("moneyline", ml), ("total", tot), ("1+ hit", hit)):
        if v is not None:
            part.append(f"{label} hold {v:.1%}")
    return ", ".join(part)


# ── matching ──────────────────────────────────────────────────────────────
def _names(team):
    """Every spelling MLB gives a club. The nickname alone is not enough:
    MLB calls Arizona "D-backs", and nobody else does."""
    if isinstance(team, str):
        team = {"teamName": team}
    team = team or {}
    return [n for n in (norm(team.get("teamName")), norm(team.get("name")),
                        norm(team.get("clubName"))) if len(n) >= 4]


def _is(team, label):
    lab = norm(label)
    return any(n in lab for n in _names(team))


def find_game(cache, home, away, start):
    """The DraftKings event for one MLB game. Both clubs must appear on the
    right side ("Nationals" in WASHINGTON_NATIONALS_MLB), and on a doubleheader
    the start time picks between the two. `home` and `away` are MLB team
    objects, or a bare nickname."""
    cands = [e for e in (cache or {}).get("events", [])
             if _is(home, e.get("home")) and _is(away, e.get("away"))]
    if not cands:
        return None

    def gap(e):
        try:
            t1 = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat((start or "").replace("Z", "+00:00"))
            return abs((t1 - t2).total_seconds())
        except (KeyError, ValueError):
            return float("inf")
    best = min(cands, key=gap)
    return best if gap(best) <= 4 * 3600 else None


def game_prices(ev_):
    """What games.py carries into its payload: moneyline and total, or None."""
    if not ev_ or not (ev_.get("ml") or ev_.get("total")):
        return None
    return {"ml": ev_.get("ml"), "total": ev_.get("total")}


def hit_price(ev_, name):
    return ((ev_ or {}).get("hits") or {}).get(norm_name(name))


if __name__ == "__main__":
    from players import baseball_today
    c = load(baseball_today())
    print(f"{len(c['events'])} games cached as of {c.get('at')}: {summary(c['events'])}")
