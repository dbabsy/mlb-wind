#!/usr/bin/env python3
"""
Pull the two raw inputs the wind profile needs and cache them on disk.

  * Statcast fly balls  — Baseball Savant CSV export, one request per month
    (a month is ~5k rows, comfortably under Savant's ~25k response cap).
  * Per-game wind       — MLB's schedule endpoint hydrated with weather, which
    returns a whole season in one request instead of 2,400 game feeds.

Cached files are keyed by month/season and skipped if already present, so
re-runs are cheap and a partial run can be resumed.

    python3 fetch.py                 # default seasons
    python3 fetch.py --years 2024 2025
    python3 fetch.py --force         # ignore cache
"""

import argparse
import csv
import gzip
import io
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

CACHE = Path(__file__).parent / ".cache"
SAVANT = "https://baseballsavant.mlb.com/statcast_search/csv"
STATS = "https://statsapi.mlb.com/api/v1/schedule"
UA = {"User-Agent": "Mozilla/5.0 (wind-profile)", "Accept-Encoding": "gzip"}

# Regular season only; March openers and October make the edges.
MONTHS = [(3, "03-01", "03-31"), (4, "04-01", "04-30"), (5, "05-01", "05-31"),
          (6, "06-01", "06-30"), (7, "07-01", "07-31"), (8, "08-01", "08-31"),
          (9, "09-01", "09-30"), (10, "10-01", "10-31")]

# Only these columns are kept; the raw export is 119 wide and mostly irrelevant.
KEEP = ["game_pk", "game_date", "game_year", "home_team", "player_name",
        "launch_speed", "launch_angle", "hit_distance_sc", "hc_x", "hc_y", "events"]


def fetch(url, tries=3, timeout=240):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 3 * attempt)
    raise last


def statcast_month(year, start, end, force=False):
    """One month of fly balls, trimmed to the columns we use."""
    out = CACHE / f"sc_{year}_{start[:2]}.csv"
    if out.exists() and not force:
        return sum(1 for _ in out.open()) - 1

    q = urllib.parse.urlencode({
        "all": "true", "hfGT": "R|", "hfBBT": "fly_ball|",
        "game_date_gt": f"{year}-{start}", "game_date_lt": f"{year}-{end}",
        "type": "details", "player_type": "batter",
    })
    raw = fetch(f"{SAVANT}?{q}")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))

    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=KEEP)
        w.writeheader()
        for r in rows:
            if r.get("game_pk"):
                w.writerow({k: r.get(k, "") for k in KEEP})
    return len(rows)


def wind_season(year, force=False):
    """Every regular-season game's wind for one year, in one request."""
    out = CACHE / f"wind_{year}.json"
    if out.exists() and not force:
        return len(json.loads(out.read_text()))

    q = urllib.parse.urlencode({
        "sportId": 1, "gameType": "R",
        "startDate": f"{year}-03-01", "endDate": f"{year}-11-15",
        "hydrate": "weather,venue(location)",
    })
    j = json.loads(fetch(f"{STATS}?{q}", timeout=120).decode("utf-8"))
    games = {}
    for d in j.get("dates", []):
        for g in d.get("games", []):
            w = g.get("weather") or {}
            if not w.get("wind"):
                continue
            ven = g.get("venue") or {}
            games[str(g["gamePk"])] = {
                "wind": w.get("wind", ""),
                "temp": w.get("temp", ""),
                "cond": w.get("condition", ""),
                "venue": ven.get("name", ""),
                "venue_id": ven.get("id"),
            }
    out.write_text(json.dumps(games, separators=(",", ":")))
    return len(games)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024, 2025, 2026])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    CACHE.mkdir(exist_ok=True)

    total = 0
    for year in a.years:
        n = wind_season(year, a.force)
        print(f"{year}  wind: {n:5d} games", flush=True)
        for _m, start, end in MONTHS:
            try:
                c = statcast_month(year, start, end, a.force)
            except Exception as e:  # noqa: BLE001
                print(f"      {start[:2]}: FAILED {type(e).__name__}: {e}", flush=True)
                continue
            total += c
            print(f"      {start[:2]}: {c:5d} fly balls", flush=True)
    print(f"\ncached {total} fly balls in {CACHE}")


if __name__ == "__main__":
    sys.exit(main())
