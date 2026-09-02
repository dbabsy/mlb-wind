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

**Park factors are split by batter handedness, and the split is centred.** A
ballpark is not the same place to the two sides of the plate — measured on
2016-2026 the mean left/right gap is ~0.010 HR per fly ball and reaches 0.043,
and the fit rediscovers known geometry unprompted (Fenway favours righties,
Yankee Stadium and Camden favour lefties, Oracle favours righties). The
handedness cells are kept *separate* from the wind cells rather than splitting
them — a wall's distance does not depend on the wind, and splitting would halve
every sample — and are **centred on each park's own mean**, because the wind
cells already carry how the park plays overall. Multiplying uncentred cells
would count the park twice.

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

**GitHub's scheduler does not work on this repo — do not try to fix it with
cron.** Measured across several days it delivered ~15% of requested runs, then
none at all for 47 hours, with the workflow active and Actions healthy.
Reducing the request rate changed nothing. The build is triggered by an
external cron service hitting the dispatch endpoint instead; see
`data/SCHEDULING.md`. The `schedule:` block is left in `build.yml` only as a
free backstop. `workflow_dispatch` has never failed:

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

**A hit is scored from that game's box score, never from the day's totals.**
Day-wide hitting stats key on date and player, which on a doubleheader date is
two games and one answer: both rows get "did he hit at all today", which is the
easier question, and game two's row gets stamped before game two starts — which
in turn freezes a prediction the late-lineup logic still needed to revise. This
bit us on 2026-08-29 (two doubleheaders, 12 contaminated rows, one game left
holding 4 hit picks instead of 3). `score()` fetches one box score per finished
game instead. `test_ledger.py` pins it.

**The ledger is committed by CI** with `[skip ci]` in the message. Without that
the commit triggers another build, forever. Expect to rebase over bot commits
when pushing; the ledger conflict is normally resolved in favour of whichever
copy has more `result` values filled in.

**That commit races with any other push.** The build runs for minutes, so a
push landing meanwhile rejects it. The step retries by taking the newer main
and re-deriving the ledger on top (record and score are idempotent), and never
fails the build — the pages must still deploy. Getting this wrong is expensive:
a lost ledger commit loses that day's frozen predictions, and they cannot be
re-recorded once the games have started.

## Open questions

- Whether the xBA correction actually helps. Year-over-year evidence says yes
  (+0.030 r); a 10-day backtest could not resolve it. The ledger will settle it
  once it has a few thousand picks.
- Hit picks backtested at 74%; through 2026-09-01 they are 68.8% against a
  mean projection of 71.2% (n=282). The gap is 0.9 SE — nothing to act on yet.
- **Do not read a Brier skill score off the hit picks.** They are the top 3 of
  each game, so the projections span 0.656–0.784 (sd 0.024). Discrimination is
  capped at `var(p) / p(1-p)` = 0.3% no matter how good the model is, and noise
  swamps that at any sample this page will ever reach. Calibration is the only
  number on that card worth reading. Matchup picks are not range-restricted the
  same way (sd 0.105, cap 4.5%) and are running at 6.0% — that card's skill
  score does mean something.
- Hit outcomes are 1.5x overdispersed across days (chi2 9.2 on 6 df, p~.16), so
  the effective sample may be ~2/3 of the nominal one. Seven days is too few to
  say. If it holds up, every confidence interval on the hits card is too narrow.
  Teammates within a game are *not* correlated (overdispersion 0.97), so if the
  effect is real it is a slate-wide thing, not a lineup thing.
- Run projections were unmeasurable until 2026-09-02: the ledger kept who won
  and threw the final score away. It now stores `sHome`/`sAway` and the accuracy
  page grades bias and MAE on each side, the total, and the margin. The margin
  is the one to watch — it is the direct check on modelling home field as a
  logit shift rather than as runs.
- Nothing in the first 8 days refutes any model coefficient. Both probability
  models are calibrated within noise (hits z=-0.86, matchups z=+0.19). The
  adjustments made on 2026-09-02 were to the measurement, not to the models.
