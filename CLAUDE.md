# mlb-wind

Five MLB analysis pages, published to GitHub Pages at
https://dbabsy.github.io/mlb-wind/ and rebuilt on a schedule.

| Script | Page | What it answers |
|---|---|---|
| `wind.py` | `index.html` | How far fly balls carry at each park, by wind direction and spray angle |
| `daily.py` | `daily.html` | How today's park and weather tilt each game's run environment |
| `players.py` | `players.html` | Per-hitter prop probabilities and starting-pitcher lines |
| `games.py` | `games.html` | Projected score and win probability per matchup |
| `hits.py` | `hits.html` | The likeliest hitters to record a hit, top 3 per game |
| `ledger.py` | `accuracy.html` | Keeps score of what the projections actually did |

`fetch.py` pulls the raw data; `orient.py` fits ballpark orientations once.
Sibling repos `mlb-streaks`, `nba-streaks`, `nfl-streaks` are standalone
single-script streak scanners and share nothing with this one.

## Run it

```bash
python3 fetch.py              # Statcast + per-game wind, cached in .cache/
python3 orient.py             # skips if data/orient.json exists; --force to refit
python3 hits.py --out hits.html
python3 ledger.py --all --hits-html public/hits.html --games-html public/games.html
```

Any page script takes `--date YYYY-MM-DD` and `--out PATH`. Building a past
date is how the models were backtested.

## Decisions that took measurement to reach

Do not undo these without re-measuring. Each cost real work to establish and
several are counter-intuitive.

**Expected stats belong on batting average, never on BABIP.** Pushing xBA into
the BABIP component measured *worse* than doing nothing (r .315 vs .414),
because BABIP rides on speed and spray that exit velocity cannot see. At the
BA level an even blend of xBA and the model's own estimate beats either alone
(.514 vs .495 and .484, n=213 hitters over consecutive seasons).

**Sprint speed sets the BABIP prior, it is not a bonus.** A hitter's own BABIP
already contains his legs; adding speed on top double-counts. Speed changes
what a thin sample regresses *toward*, measured at ~+0.006 BABIP per ft/s.

**Home-field advantage is a probability shift, not extra runs.** Measured over
1,975 games: home clubs win ~52.8% while outscoring visitors by 0.05 runs.
Batting last is worth far more than the scoring, so it is applied as a logit
shift in `games.py`, never by inflating home runs scored.

**Batter and pitcher rates combine by odds ratio (log5), not multiplication.**
Multiplying double-counts the league average and can exceed 1.

**Each rate is regressed by its own stabilisation point** (`hits.py: REG`) —
strikeouts settle in ~60 PA, BABIP in many hundreds of balls in play. One
blanket constant trusts BABIP far too early.

**The All-Star Game is filed under "Regular Season"** by ESPN and MLB alike,
with fake teams. `wind.py` screens it out by checking the opponent against the
real franchise list. Without that, exhibition stats pollute the park model.

**Game timestamps are UTC; the slate rolls on Central.** A build after 7pm CT
would otherwise show tomorrow's games while tonight's are still being played.

## Gotchas that have bitten before

**GitHub's scheduler is unreliable here.** It has delivered as little as 15% of
requested runs, and once none for 20 hours straight. `workflow_dispatch` has
never failed. If pages look stale, dispatch manually:

```bash
gh workflow run "Build and deploy" --ref main
```

**Pushes sometimes do not trigger a build at all** — no run, no error. Verify
after pushing; dispatch if nothing appears.

**Two caches make the build fast. Do not remove them.** `data/orient.json`
(committed) saves ~5 minutes a build; `.cache/model.json` (fingerprinted
against the raw data) saves the park model rebuild. Build is ~2.5 min with
both, ~9 min without.

**The ledger must never record a game that has started.** `ledger.py` refuses
any game whose first pitch has passed, freezes rows once a game begins, and
drops picks that a late lineup change superseded. Breaking any of those turns
the accuracy page into a lie. There is a manual check for this: try recording
a past date and confirm it writes nothing.

**The ledger is committed by CI** with `[skip ci]` in the message. Without that
the commit triggers another build, forever. Expect to rebase over bot commits
when pushing; the ledger conflict is normally resolved in favour of whichever
copy has more `result` values filled in.

## Open questions

- Whether the xBA correction actually helps. Year-over-year evidence says yes
  (+0.030 r); a 10-day backtest could not resolve it. The ledger will settle it
  once it has a few thousand picks.
- Hit picks backtested at 74% but the first live night came in at 56% (n=43).
  Too early to mean anything either way.
- Calibration converges long before skill score. Roughly 300 picks before the
  calibration gap is worth reading; thousands before Brier skill is.
