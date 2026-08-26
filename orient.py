#!/usr/bin/env python3
"""
Work out which way each ballpark points.

Nothing in the MLB or weather APIs states a park's orientation, but it can be
recovered: MLB records a human-written wind label for every game ("Out To LF"),
and the weather archive records the true compass wind for that same hour. The
offset between the two IS the park's bearing from home plate to center field.
Averaging that offset over thousands of games pins it down.

The fit checks itself — once a bearing is known, every game's label can be
re-derived from the compass and scored against what MLB actually wrote.

    python3 orient.py            # fit once, store in data/orient.json
    python3 orient.py --force    # refit from scratch
"""

import argparse
import json
import math
import ssl
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

CACHE = Path(__file__).parent / ".cache"
# A ballpark's orientation is a fact about the ground it sits on, not a
# statistic — it does not change between builds. Keeping it in the repository
# rather than a cache means it is fitted once, survives cache eviction, and is
# reviewable in the diff.
STORE = Path(__file__).parent / "data" / "orient.json"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
UA = {"User-Agent": "Mozilla/5.0 (wind-profile)"}

# Where the wind is heading, relative to the home-plate -> center-field axis.
# Left field sits counter-clockwise of center, right field clockwise.
LABEL_OFFSET = {
    "Out To CF": 0.0,
    "Out To LF": -35.0,
    "Out To RF": 35.0,
    "In From CF": 180.0,
    "In From LF": 145.0,
    "In From RF": 215.0,
    "L To R": 90.0,
    "R To L": -90.0,
}
# Only trust the label when the wind is strong enough to be legible.
MIN_MPH = 8.0
FIT_SEASONS = ("2022", "2023", "2024", "2025")


def get(url, timeout=90):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read())


def parse_wind(s):
    mph, _, rest = (s or "").partition("mph")
    try:
        speed = float(mph.strip())
    except ValueError:
        speed = 0.0
    return speed, rest.lstrip(", ").strip()


def circular_mean(degs):
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if x == 0 and y == 0:
        return None, 0.0
    mean = math.degrees(math.atan2(y, x)) % 360
    # Resultant length: 1.0 means every sample agreed, 0 means pure noise.
    r = math.hypot(x, y) / len(degs)
    return mean, r


def load_games():
    """Games with a usable directional label, grouped by venue."""
    by_park = defaultdict(list)
    for p in sorted(CACHE.glob("wind_*.json")):
        if p.stem.split("_")[1] not in FIT_SEASONS:
            continue
        for g in json.loads(p.read_text()).values():
            speed, label = parse_wind(g.get("wind"))
            if (label not in LABEL_OFFSET or speed < MIN_MPH
                    or not g.get("lat") or not g.get("start")):
                continue
            by_park[g["venue_id"]].append(g)
    return by_park


def hourly_series(lat, lon, start, end):
    q = urllib.parse.urlencode({
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "start_date": start, "end_date": end,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "mph", "timezone": "UTC",
    })
    h = get(f"{ARCHIVE}?{q}")["hourly"]
    return {t[:13]: (s, d) for t, s, d in
            zip(h["time"], h["wind_speed_10m"], h["wind_direction_10m"])}


def load_orientations():
    """Fitted bearings, from the repo first and the old cache location second."""
    for path in (STORE, CACHE / "orient.json"):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                continue
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="refit even when a stored fit already exists")
    ap.add_argument("--min-parks", type=int, default=25)
    a = ap.parse_args()

    have = load_orientations()
    if have and len(have) >= a.min_parks and not a.force:
        print(f"{len(have)} parks already fitted in {STORE.name}; skipping "
              f"(pass --force to refit)")
        return

    by_park = load_games()
    print(f"{len(by_park)} parks with labelled windy games", flush=True)

    out = {}
    for vid, games in sorted(by_park.items(), key=lambda kv: -len(kv[1])):
        if len(games) < 40:
            continue
        name = games[0]["venue"]
        dates = sorted(g["start"][:10] for g in games)
        try:
            series = hourly_series(games[0]["lat"], games[0]["lon"], dates[0], dates[-1])
        except Exception as e:  # noqa: BLE001
            print(f"  {name[:28]:30s} archive failed ({type(e).__name__})", flush=True)
            continue

        # Each game votes for a bearing: where the wind was heading, minus
        # where the label says that is relative to center field.
        votes, samples = [], []
        for g in games:
            hour = datetime.fromisoformat(g["start"].replace("Z", "+00:00")).strftime("%Y-%m-%dT%H")
            hit = series.get(hour)
            if not hit or hit[1] is None or hit[0] is None or hit[0] < MIN_MPH / 2:
                continue
            _speed, from_deg = hit
            toward = (from_deg + 180.0) % 360.0
            _s, label = parse_wind(g["wind"])
            votes.append((toward - LABEL_OFFSET[label]) % 360.0)
            samples.append((toward, label))
        if len(votes) < 30:
            continue

        bearing, agree = circular_mean(votes)

        # Self-check: re-label every game from the compass and the fitted
        # bearing, and see how often that reproduces what MLB wrote.
        correct = 0
        for toward, label in samples:
            rel = (toward - bearing + 180) % 360 - 180
            best = min(LABEL_OFFSET, key=lambda k: abs((rel - LABEL_OFFSET[k] + 180) % 360 - 180))
            correct += best == label
        acc = correct / len(samples)

        out[str(vid)] = {"name": name, "bearing": round(bearing, 1),
                         "n": len(votes), "agree": round(agree, 3), "acc": round(acc, 3)}
        print(f"  {name[:28]:30s} CF bearing {bearing:5.1f}deg  "
              f"n={len(votes):4d} agreement={agree:.2f} relabel={acc:.0%}", flush=True)

    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(out, indent=1, sort_keys=True))
    (CACHE / "orient.json").write_text(json.dumps(out, indent=1, sort_keys=True))
    if out:
        mean_acc = sum(v["acc"] for v in out.values()) / len(out)
        print(f"\n{len(out)} parks fitted · mean relabel accuracy {mean_acc:.0%}")


if __name__ == "__main__":
    main()
