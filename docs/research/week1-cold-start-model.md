# Week-1 cold-start projection model — 2026-09-01

Built as `backend/week1.py`, wired into `generate_report.py` as a third
projection tier that only activates when `--week 1`.

## 1. The problem

`projections.py` deliberately refuses to reach into the prior season — "a
player's role can change completely between seasons"
(`docs/design/in-house-projection-model-spec.md` §3.2). That is a sound default
for a rolling average, but it means that in week 1 every player has
`games_used = 0`, lands in the `no_data` tier, and receives a flat positional
baseline.

Measured over the 2021–25 week 1s, that fallback:

- **ranks nobody**: pairwise start/sit accuracy 0.500 — an exact coin flip,
  since every player at a position gets the identical number
- MAE 6.11, RMSE 7.63, R²oos 0.122

Week 1 is also the single week where the tool is most likely to be consulted.

## 2. Approach

A per-position ridge regression over features available *before any football is
played*. Everything comes from `nflreadpy`, free, no key.

| block | features |
|---|---|
| prior-season production | ppg, last-8 ppg, ppg sd, ppg two seasons back |
| prior-season volume / role | targets, carries, attempts, receptions, target share, WOPR, rush/rec/pass yards |
| durability | games played, availability rate, games two seasons back |
| pedigree | draft pick, draft round, rookie flag, age, years of experience |
| role at kickoff | week-1 depth-chart rank, team-change flag |
| game context | implied team total, total line, spread line, home/away |
| opponent | prior-season defence-vs-position, 50% regressed |

Design choices worth stating:

- **Per position, not pooled.** Positions differ in scale by 3× and the features
  mean different things across them (`carries` is workload for an RB, mobility
  for a QB). A pooled fit spends its capacity on the between-position gap we
  already know.
- **Missing is a state, not a zero.** A rookie has no prior season. Every
  feature is imputed at the position's training median *and* flagged with a
  companion `_missing` indicator, so the fit can learn "we don't know this" as
  distinct from "this is average".
- **Undrafted is data.** `draft_pick` fills to 300 rather than the median —
  going undrafted is the signal, and it belongs at the end of the scale.
- **As-of discipline throughout.** Features for season *N* only ever read
  seasons *N−1* and *N−2*. The 2025+ depth-chart feed is a stream of timestamped
  snapshots that extends past the season, so `_load_depth_ranks` filters to
  snapshots on or before week-1 kickoff — an unfiltered read would hand a
  week-1 projection a depth chart from the following March.
- **No new dependency.** The ridge is a 56-column normal equation on a few
  hundred rows; it is solved in ~40 lines of plain Python rather than adding
  scikit-learn. A gradient-boosting version was tested and was marginally worse
  on RMSE (6.40 vs 6.35), so the simpler model is also the better one here.

## 3. Feature value — what actually carries the signal

Permutation importance (RMSE, held-out 2024 + 2025), top features:

```
draft_pick     0.456      total_line     0.057
prior_ppg      0.419      implied total  0.054
depth_rank     0.235      prior_last8    0.044
prior2_ppg     0.136      prior_rush_yd  0.036
years_exp      0.066      dvp_prior      0.036
```

Block ablation — RMSE cost of removing each block entirely:

| block dropped | ΔRMSE |
|---|---|
| **Vegas game context** | **+0.169** |
| **draft capital + rookie flag** | **+0.156** |
| prior-season production | +0.065 |
| age / experience | +0.060 |
| prior-season volume / role | +0.044 |
| team change | +0.017 |
| durability | +0.007 |
| depth chart | +0.000 |
| **opponent DvP** | **−0.000** |

### On "opponent strength"

The honest result: **a defence-vs-position rate carried over from last season is
worth nothing** once the betting line is in the model. That is not a null
result about matchups — it is a result about *how to encode them*. The closing
line already prices the opponent, and prices this season's version of them
(after the draft, free agency, and coaching changes) rather than last season's.
`dvp_prior` is kept in the feature set so the ablation stays reproducible and a
report view has a matchup label to display, but the Vegas block is what carries
the matchup signal.

Vegas lines are available for week 1 on `nflreadpy.load_schedules()`
(`spread_line`, `total_line`); implied team total = (total ± spread) / 2.

## 4. Results — walk-forward, train on every prior season

| held-out week 1 | n | MAE | RMSE | bias | ρ | pairwise acc | baseline MAE | baseline pairwise |
|---|---|---|---|---|---|---|---|---|
| 2021 | 341 | 5.23 | 6.95 | −0.13 | 0.645 | 0.728 | 6.63 | 0.500 |
| 2022 | 341 | 4.87 | 6.34 | +0.76 | 0.619 | 0.697 | 5.96 | 0.500 |
| 2023 | 339 | 4.98 | 6.61 | +1.65 | 0.562 | 0.696 | 5.96 | 0.500 |
| 2024 | 339 | 4.69 | 5.99 | +1.06 | 0.664 | 0.724 | 6.04 | 0.500 |
| 2025 | 354 | 4.35 | 5.80 | +0.46 | 0.666 | 0.716 | 5.97 | 0.500 |

**Pooled over 1 714 held-out player-weeks:**

| metric | model | positional-mean baseline | paired p |
|---|---|---|---|
| MAE | **4.82** | 6.11 (−21.1%) | 5.3e−41 |
| RMSE | **6.35** | 7.63 | 5.5e−27 |
| R²oos | **0.392** | 0.122 | — |
| Spearman ρ | **0.63** | 0.00 | — |

Per position:

| position | n | MAE (baseline) | ρ | pairwise acc | calibration slope |
|---|---|---|---|---|---|
| QB | 189 | 7.88 (8.93) | 0.430 | 0.650 | 0.996 |
| RB | 454 | 4.24 (6.27) | 0.697 | 0.754 | 1.138 |
| WR | 726 | 5.07 (6.26) | 0.568 | 0.704 | 0.841 |
| TE | 345 | 3.38 (4.04) | 0.507 | 0.687 | 0.809 |

For scale: the in-season rolling-average model reaches pairwise accuracy 0.733
with a full six games of current-season data behind it. **The week-1 model gets
to 0.711 with none** — most of the ranking value is recoverable before a snap is
played.

## 5. How much offseason signal survives

Prior-season per-game points vs actual week-1 points, pooled 2016–25:

| position | r | ρ | R² | prior target-share ρ | prior games-played ρ |
|---|---|---|---|---|---|
| RB | 0.604 | 0.659 | 0.365 | 0.541 | 0.312 |
| WR | 0.550 | 0.601 | 0.303 | 0.590 | 0.244 |
| TE | 0.484 | 0.494 | 0.235 | 0.503 | 0.321 |
| QB | 0.334 | 0.314 | 0.112 | — | 0.318 |

Two things stand out. For WR and TE, prior **target share** is a better week-1
predictor than prior **points** (ρ 0.590 vs 0.550; 0.503 vs 0.484) — role
carries across an offseason better than scoring does, the same volume-vs-points
stability result found in the in-season audit. And prior games played predicts
week-1 points at ρ ≈ 0.24–0.32 across every position: durability is a real,
separable signal, not a proxy for talent.

## 6. Two guards that mattered

A linear model extrapolates without bound, and week-1 inputs are unbounded. The
first working version produced a **175.8-point TE projection** — an out-of-range
input standardised to a huge z-score and multiplied out. That single row tripled
its season's RMSE (10.8 vs ~6.6 everywhere else) and was fully responsible for
the pooled RMSE looking 18% worse than it really was.

Both fixes are applied at predict time, so the fit stays a plain ridge:

- clamp every standardised feature to ±4 sd of the training mean
- cap the output at the highest week-1 score the position produced in training

After the guards: pooled RMSE 7.49 → **6.35**, worst-season RMSE 10.81 → **6.95**.
Both are covered by regression tests in `tests/test_week1.py`.

The ridge penalty was then swept at 25/60/120/250/500/1000/2000. MAE and RMSE
are nearly flat from 25 to 250; **α = 120** was chosen because it minimises mean
|calibration slope − 1| across the four positions (0.123, vs 0.170 at α = 25) at
no cost on either error metric.

## 7. Integration

`generate_report.py` gains an optional `week1_projections` parameter with the
same precedence and the same "None means arithmetically identical to before"
contract as `consensus_projections`:

```
FantasyPros consensus  >  week-1 model  >  in-house rolling average
```

It is built only when `--week 1`, and `--skip-week1-model` disables it (falling
back to the old `no_data` baseline). The tier is labelled **"Week 1 estimate"**
/ source **"In-house model (pre-season)"**, distinct from the rolling average,
per the standing rule that different kinds of number get different labels.

Verified end-to-end against real 2025 data: 971 of 1 053 players projected by
the cold-start model (the remaining 82 are DST/K, which this model does not
cover, plus players absent from the roster snapshot).

Fixed along the way: `load_player_pool_nflreadpy` guarded a missing player id
with `if not player_id`, which does not catch `float("nan")` — NaN is truthy.
One unidentifiable roster row therefore reached the fixture as
`"playerId": NaN` and made the whole file fail `json.dumps(allow_nan=False)`,
losing every projection for the week.

## 8. Known limits

- **Conditional on playing**, exactly like `projections.py` — training rows only
  exist for players who actually played week 1. See §6 of
  `scoring-engine-and-model-audit.md`: multiply by an availability probability
  before comparing players with different injury risk. Week 1 is the worst week
  for this, since preseason injury news is at its noisiest.
- **QB is the weak position** (ρ 0.43, MAE 7.88) — same ordering as in-season,
  where QB also has the lowest ICC (0.25). Starter changes and rookie QBs are
  most of it.
- **DST and K are not covered.** They fall back to the existing `no_data`
  baseline, as before.
- **`load_ff_rankings()` is not used.** It carries FantasyPros preseason ECR,
  which would very likely be the strongest single week-1 feature — but it is a
  current-snapshot-only feed with no history, so it cannot be validated
  walk-forward. It is usable *live* for an upcoming week 1 and is the most
  promising addition; it just cannot be backtested from this data.
- **Retrained per run**, not persisted: the fit is a few hundred rows per
  position and takes well under a second, so there is no artefact to version.
