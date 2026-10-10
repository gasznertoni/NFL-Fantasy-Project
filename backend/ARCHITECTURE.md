# Backend architecture — what every module does and why

This is the single reference for the backend's design. The code itself carries
only one-line docstrings and a handful of short trap warnings; the reasoning,
the measurements behind each constant, and the history of what went wrong live
here. When you change a module, update its section.

`backend/README.md` is the shorter build log; `CLAUDE.md` at the repo root is the
project brief and changelog. Research write-ups referenced below live in
`docs/research/` and design docs in `docs/design/`.

---

## Contents

1. [How the pieces fit](#1-how-the-pieces-fit)
2. [Conventions that apply everywhere](#2-conventions-that-apply-everywhere)
3. [Scoring and stat lines](#3-scoring-and-stat-lines) — `scoring.py`, `dst.py`, `kicker.py`
4. [The in-season estimator](#4-the-in-season-estimator) — `projections.py`, `baseline.py`, `calibration.py`, `calibration_fit.py`, `blend.py`, `context.py`, `ridge.py`, `entity_prior.py`, `expected_td.py`
5. [Week 1](#5-week-1) — `week1.py`, `week1_kdst.py`
6. [Availability](#6-availability) — `availability.py`, `depth_charts.py`, `espn_injuries.py`
7. [The consensus tier](#7-the-consensus-tier) — `fantasypros.py`, `rotowire_projections.py`
8. [Built, measured, and left off](#8-built-measured-and-left-off) — `matchup.py`, `usage.py`, `rotowire.py`
9. [News](#9-news) — `news.py`
10. [Report generation](#10-report-generation) — `generate_report.py`, `waiver_targets.py`, `season_week.py`, `generate_team_config.py`
11. [Evaluation](#11-evaluation) — `track_record.py`, `generate_track_record.py`, `backtest.py`, `csv_backtest.py`
12. [Storage](#12-storage) — `store.py`

---

## 1. How the pieces fit

`generate_report.py` is the only entry point that matters week to week. It is a
script, not a service: it loads everything, fits what needs fitting, and writes
the JSON fixtures the frontend reads (`weekly-report-week-N.json`,
`player-pool.json`, `manifest.json`).

```
nflreadpy / ESPN / Sleeper / FantasyPros / Rotowire
        │
        ▼
 stat lines ──► scoring.py (league points, from each league's config)
        │            ▲            ▲
        │       dst.py       kicker.py
        ▼
 projection tiers, in precedence order:
   1. consensus      fantasypros.py + rotowire_projections.py (top 10 / position)
   2. week-1 model   week1.py (QB/RB/WR/TE), week1_kdst.py (K, D/ST)   — week 1 only
   3. in-house       projections.py
                       ├─ shrinkage target  baseline / entity_prior.py
                       ├─ k, affine, bands  calibration.py (fitted by calibration_fit.py)
                       ├─ refinement        blend.py (+ context.py, expected_td.py)
                       └─ estimator series  expected_td.py (off)
        │
        ▼
 × P(play)   availability.py (+ depth_charts.py, espn_injuries.py)
        │
        ▼
 news flags (news.py), waiver targets (waiver_targets.py)
        │
        ▼
 fixtures  ──►  track_record.py / generate_track_record.py (grading)
               store.py (cache + append-only prediction log)
```

Every projection, whatever tier produced it, is a **stat line scored through the
league's own config** — never a vendor's precomputed points field.

---

## 2. Conventions that apply everywhere

**Pure core, adapter shell.** Each module puts pure, injectable logic at the top
(tested with synthetic rows, no network) and its network/nflreadpy adapters at
the bottom (`load_*`, `fetch_*`), which the test suite does not exercise. Every
nflreadpy/pandas/pyarrow/anthropic import sits *inside* a function, which is why
the test suite runs on `pytest` + `requests` alone (`requirements-dev.txt`). Do
not hoist those imports to module scope.

**Degrade, never crash — but say so.** Optional layers (news, consensus,
availability, blend, Rotowire, week-1 models, the store) catch their own failures
and fall back to the behaviour that existed before they did. The lesson learned
the hard way is that a silent degrade is indistinguishable from "nothing to
report": the news layer failed for every player for months behind a bare
`except`. Every degrade now logs or counts what happened.

**NaN is not None.** nflreadpy comes through pandas, so an absent cell is
`float("nan")`. NaN is truthy, is not `None`, and does not equal itself. Guards
written as `if not x` or `x is None` let it through. The test is `x != x`. This
trap has shipped at least five times: `"playerId": NaN` failing a whole fixture
(v16), a NaN stat propagating to a NaN total (v18), every unplayed game reading
as played so `season_week.py` returned week 18 (v19), a NaN points-allowed
matching no D/ST tier band, and the same `is None` check in `dst.py`'s
points-allowed join (third audit). A test row that spells "missing" as `None`
will pass while production is wrong.

**A column-map key that matches nothing is silent.** Stat-line mappers treat an
absent column as zero by design, so a misspelled column produces a
plausible-looking wrong number. Check new keys against a live
`load_player_stats()` response and reconcile against an external reference, not
against our own recomputed actuals (a scoring bug contaminates both sides of an
internal backtest).

**As-of discipline.** Anything that projects week *w* uses only data from before
week *w*. Calibration pieces are fitted on completed prior seasons only.

**Expected vs conditional.** `projected_points` / `points` is an *expected*
value: P(play) × points-if-he-plays. `conditional_points` /
`conditionalPoints` is the if-he-plays number. Every estimator here is fitted on
games that were played, so its raw output is conditional. Mixing the two up is
the single easiest mistake a downstream consumer can make.

**Measure through the shipped stack.** A component that beats a bare rolling
average can be worth nothing once shrinkage, recalibration and the blend are
around it. Accept a change only when RMSE *and* ranking (pairwise start/sit
accuracy, Spearman) move together, measured end to end on held-out seasons.

**A config that defines two overlapping category families double-counts.**
`kicker.validate_fg_band_family`, `kicker.validate_fg_miss_band_family` and
`dst.validate_dst_td_categories` raise on that, and
`generate_report.validate_scoring_config` runs all of them before any point is
computed. Until the third audit they ran only from tests.

---

## 3. Scoring and stat lines

### `scoring.py` — league points from a raw stat line

The one function every tier goes through: `compute_league_points(stat_line,
scoring_config, position=None)`. Config-driven, so a league's rules are data,
not code. Returns a `ScoringResult` with a per-category breakdown so a view or
test can show *why* a player scored what he did.

The config has three blocks, any of which may be empty:

- **`linear`** — `category × value`. A category missing from the stat line
  contributes 0 (a QB line has no return yards; absence is not an error).
- **`milestones`** — yardage bonuses. Each category is
  `{"mode": "highest"|"cumulative", "tiers": [{"threshold", "points"}, …]}`.
  `mode` lives once per category so a hand edit cannot leave tiers disagreeing.
  "highest" (default) pays only the top tier met; "cumulative" pays every one.
- **`tiers`** — banded D/ST scoring (points allowed, yards allowed): the
  lowest-`max` band the value fits under. Bands sorted ascending; the last omits
  `max` as the catch-all. A NaN value matched no band (`nan <= max` is False for
  every band) and silently dropped up to 5 points; now guarded.

**Position-scoped values** (`resolve_value`). A value may be
`{"TE": 1, "default": 0.5}`: league-1 pays a TE reception 1.0 and everyone else
0.5, and prices receiving milestones TE > RB/WR > QB. An unlisted position falls
to `default`; no match and no default contributes 0. `compute_league_points`
reads the position from `stat_line["position"]` unless one is passed, which is
why every producer (`backtest.load_full_pool_game_logs`, the report's game-log
loader, the D/ST and K assemblers) sets it. Build a stat line by hand without `position` and tight ends
silently score as receivers — a live bug in `blend_consensus_projections` once.
`resolve_value` is public because `kicker.py` and `dst.py` validate configs with
it. Totals are forced to float (`round(int, 2)` returns an int).

**nflreadpy column map** (`NFLREADPY_OFFENSE_COLUMN_MAP`,
`nflreadpy_row_to_stat_line`). Kept as an explicit, separately testable dict so
column drift touches only the map. Offence only; D/ST needs `dst.py`'s joins.
History worth knowing:

- Interceptions are `passing_interceptions`, not `interceptions`. The wrong key
  was mapped until 2026-09-01, so `pass_int` was never applied: every QB scored
  ~1.4 pts/game too high (1 192 unscored INTs across 1 631 starter-games,
  2023-25). Backtests could not see it because the same bug inflated the
  actuals.
- Fumbles come from nflreadpy's whole-player totals, not the sum of
  `sack_`/`rushing_`/`receiving_fumbles(_lost)`. Those are a subset that misses
  15.6% of `fumbles_total` and 10.6% of `fumbles_lost_total` (returns,
  laterals, aborted snaps).
- `special_teams_tds` → `return_td`: kick/punt return TDs by an offensive
  player, 6 points (43 across 2023-25, previously worth 0).
- `kick_return_yd` / `punt_return_yd`: league-1 scores player return yards at
  0.1/yd. Player side only; the D/ST-side return-yardage line does not exist.
- `fumble_recovery_td`: league-2's MISC "Fumble Recovery TD = 6"; league-1
  doesn't define it.
- Completions (+0.1), incompletions (−0.1) and sacks taken (−0.5) for league-1
  (2026-09-06). Incompletions have no column: derived as attempts − completions,
  NaN-guarded on both sides and floored at zero.
- Any NaN cell is treated as absent; one NaN used to fail the entire fixture at
  `json.dumps(allow_nan=False)`.

### `dst.py` — team-defence stat lines

Turns nflreadpy `load_team_stats()` and `load_schedules()` rows into the D/ST
stat line `scoring.py` already knows how to score
(`docs/research/dst-scoring-fields.md`).

- **Direct fields** off the team's own row: sacks, INTs, opponent fumbles
  recovered, safeties, forced fumbles, return yards.
- **Self-join on `game_id`** reading the *opponent's* row: blocked-kick credit
  (blocks are recorded on the team whose kick was blocked; confirmed on 64 real
  2024-25 rows) and yards allowed (opponent's passing + rushing yards).
- **Schedule join** for points allowed (100% `game_id` overlap with team stats
  in 2025). Unplayed games carry NaN scores, so `_has_final_score` uses the
  NaN-aware test; `_points_allowed_for_team_game` returns None for a team not
  in the game or a game not played.
- `def_points_allowed` / `def_yards_allowed` are always set, even at 0 — a
  shutout is information.
- `build_dst_game_logs` produces `{team: [game, …]}` in the same game-log shape
  `project_player` expects (a D/ST's playerId *is* its team abbreviation). A
  row missing its opponent or schedule partner is skipped, not guessed.
- nflreadpy says `LA` for the Rams; mapped to `LAR`. Duplicated from
  `generate_report.py` to avoid a circular import.

**Touchdown categories.** Three names for three things, so each league's config
scores exactly the rule it has: `def_td` (any defensive TD), `def_st_td`
(kick/punt/blocked-kick return TD), `def_fumble_rec_td` (fumble returned for a
score). As of 2026-09-06 both leagues credit all five defensive/ST TD lines at
6 and use `def_td` + `def_st_td`; league-1's v15 narrowing to fumble-recovery
TDs only is retired. `def_td` and `def_fumble_rec_td` overlap, so
`validate_dst_td_categories` raises if a config defines both. `def_2pt_return` is
ESPN's 2PTRET (4 across 2024-25).

**Warning on `fumble_recovery_tds`:** it is *not* a defensive column. It counts
a team's fumble recoveries returned for a score from any phase, including its
own offence, and exceeds `def_tds` on 35 team-games in 2024-25. It stays mapped
under its correct name, but do not point a D/ST rule at it.

**Adapters.** `load_schedule_with_scores_nflreadpy` pulls a superset of
`generate_report.load_schedule_nflreadpy`'s columns (adds `game_id` and scores)
rather than changing that adapter's shape. `load_dst_pool_nflreadpy` builds one
entry per team in this season's schedule (not `load_teams()`, which carries
defunct franchises).

### `kicker.py` — kicker stat lines

Kickers are ordinary `position == "K"` rows in `load_player_stats()`, so this is
a column map plus a pure row mapper with no adapter of its own —
`generate_report.load_all_game_logs_nflreadpy` merges its output with the
offence map on every row (the category names are disjoint, so no position
branch is needed).

- **Made FGs, two families.** nflreadpy's six native buckets
  (0-19/20-29/30-39/40-49/50-59/60+) are emitted 1:1 for league-2 (3/3/3/4/5/6),
  and also rolled up losslessly into league-1's bands. `fg_made_40_49` belongs to
  both. `validate_fg_band_family` raises on a config that mixes them, because it
  would score every kick twice.
- **Missed FGs, two families.** league-2 charges a flat −1. league-1 charges −2
  inside 40, −1 from 40-49 and *nothing* beyond 50. `validate_fg_miss_band_family`
  guards the mix. The native missed buckets sum exactly to `fg_missed` (140 =
  140 in 2025), so blocks are not in them and are added by distance.
- **Blocked kicks count as misses** (there is no kicker-side "blocked"
  category). Distances come from `fg_blocked_list` (`"36;44"`);
  `fg_blocked_distance` is the *sum* of distances (80 for that game) and is not
  usable. An unparseable list drops the block rather than guessing a band.

---

## 4. The in-season estimator

### `projections.py` — the in-house tier

Projects one player for (season, week) from his game log
(`docs/design/in-house-projection-model-spec.md` §3.2-3.3, §5). Data loading is
injected, so the module has no network dependency;
`load_recent_games_nflreadpy` is the thin adapter.

The chain, all inspectable on the output:
`per_game_points → rolling_avg → shrunk_avg → (blend) → affine → × P(play)`.

- **`games_before`** — the as-of filter every module reuses: games strictly
  before the target week, same season only. "Don't reach into the prior season
  by default — a role can change completely between seasons." A bye is simply
  absent from a log of games played.
- **`_rolling_average`** — exponentially decayed: newest game weight 1.0, each
  older game one more factor of `decay`. `decay=1.0` is arithmetically the plain
  mean.
- **Window and decay.** `DEFAULT_WINDOW = 8`, `DEFAULT_DECAY = 0.9`
  (2026-09-01). The earlier sweep stopped at 6; past it the improvement keeps
  going, but only when older games are discounted:

  | window | decay | MAE | RMSE | pairwise | Spearman |
  |---|---|---|---|---|---|
  | 12 | 0.9 | 4.715 | 6.762 | 0.7348 | 0.663 |
  | 8 | 0.9 | 4.726 | 6.785 | 0.7344 | 0.662 |
  | 6 | 1.0 | 4.755 | 6.833 | 0.7325 | 0.659 (old default) |
  | 4 | 1.0 | 4.834 | 6.934 | 0.7294 | 0.654 |

  8 over 12 because a shorter window reacts faster to a real role change at no
  measured cost.
- **Shrinkage.** `empirical_bayes` is the default mode: weight `n / (n + k)`
  with `k = σ²_within / σ²_between` from `calibration.py`, at *every* sample
  size. The legacy `window` mode (`_shrinkage_weight`: 0 games → pure baseline,
  partial window → `1 − strength·(1 − n/window)`, full window → no shrinkage) is
  kept only for reproducing old results. Its last branch was the defect: a full
  window was taken at par while calibration slopes were 0.735-0.878 (all
  p < 1e-11). At the old window of 6, `n/(n+k)` predicts 0.76/0.84/0.79/0.82
  against measured slopes 0.735/0.878/0.841/0.852 — two routes to the same
  numbers, which is the evidence the over-dispersion is regression to the mean.
  With no baseline supplied every mode degrades to no shrinkage.
- **`DEFAULT_SHRINKAGE_K_FALLBACK`** averages only the four *measured* skill
  positions. Averaging the whole dict would let K's k=30 drag every fallback to
  ~6.
- **`POSITION_CALIBRATION_SCALE`** is 1.0 for all positions
  (`csv_backtest.py`, 5 749 player-weeks of 2025). QB bias at 1.0 is +0.49
  pts/game (weeks 2-18), but scaling to the L2-optimal 1.031 worsens MAE
  (p=0.0005) because QB scoring is right-skewed. QB's earlier 0.85 had no
  supporting backtest and made underprojection 3.7 pts worse. RB/WR/TE positive
  biases come from boom-game outliers; scaling up worsens MAE.
- **Affine, then availability.** The rank-preserving affine correction is
  applied *before* the P(play) multiplier so it stays a statement about the
  points estimate. `E[points] = P(play) × E[points | play]`; the miss branch is
  exactly zero. The interval is scaled consistently, and `conditional_points`
  carries the if-he-plays number for the report and the eval layer.
- **`_estimator_series`** can substitute `non_td_points + exp_td_points` (from
  `expected_td.py`) for each game's total, falling back per game. That is the
  opportunity-based touchdown estimator, and it is **off**
  (`USE_OPPORTUNITY_TD_ESTIMATOR = False`): standalone it beat the rolling
  average at every position (RB p=1.2e-02, WR p=6.4e-03, TE p=3.6e-03, +0.48
  lineup pts/week), but through the real pipeline it is worth nothing and the
  sign flips between held-out seasons:

  | | season | RMSE | p |
  |---|---|---|---|
  | league-1 | 2025 | 6.4872 → 6.4763 | 0.170 |
  | league-1 | 2024 | 6.5569 → 6.5596 | 0.687 (worse) |
  | league-2 | 2025 | 6.1662 → 6.1579 | 0.135 |
  | league-2 | 2024 | 6.2659 → 6.2698 | 0.387 (worse) |

  The volume block in `blend.py` already carries what opportunity knows. Adding
  the two halves as blend features instead is worth p=0.60: a ridge cannot use a
  decomposition of a feature it already has whole. Kept, tested, flip to enable.
  `per_game_points` always shows real scores even when the estimator series
  differs.
- **Validation.** `window` must be a positive int (`eligible[-0:]` would mean
  "everything"); `shrinkage_strength` in [0, 1]; `decay` in (0, 1].
- **`project_players`** is a thin batch loop; per-player dicts
  (`positional_baselines`, `opponent_multipliers`, `usage_multipliers`,
  `calibration_scales`) default to no-op for a missing player. Position
  resolution is the caller's job.
- The adapter carries `opponent_team`, `position` and the usage columns through
  as non-scoring keys. Its `usage` import is local to avoid a circular import.

### `baseline.py` — positional baselines (legacy shrinkage targets)

Computes the scalar a thin sample is shrunk toward, injected into
`project_player` as a float. Exists because a player with zero prior games used
to project a literal 0.0, and the partial-window tier carried ~9× the bias of the
full tier (role-emerging players underprojected).

The population a baseline is built from must match the one it is applied to:

- **`thin`** (fewer than `window` prior games) — feeds the partial-window tier.
- **`debut`** (`games_played_before == 0`) — feeds the no-data tier. Needed
  because pooled "thin" flipped no-data bias from +1.8 to about −5.4: a player on
  his fifth game already scores like a starter.
- **`min_week`** excludes week 1 from the debut population
  (`DEFAULT_DEBUT_MIN_WEEK`). Week 1 is when the whole league debuts at once: in
  2025, 354 of 611 players debuted in week 1 averaging 6.56 points, against 157
  debuting in week 5+ averaging 2.00 — close to the no-data tier's actual ~1.8.
  Without this, "debut" still overshot (bias −3.6). Opt-in; defaults to off.
- **`all`** is kept so `backtest.py` can show it is wrong rather than the docs
  merely asserting it.
- Prior-season fallback when the current season has no eligible games, tagged
  `source="prior_season"`; `no_data` returns `baseline=None` (no shrinkage).
- `positional_baselines_by_week` is the one-pass batch version and must agree
  with calling `positional_baselines` per week (tested).
  `baselines_by_player_week_for_shrinkage` picks debut or thin per player-week
  by what `project_player` will actually use; full-window players are omitted.
- `DEFAULT_BASELINE_STATISTIC = "mean"` (bias-optimal); `median` is the MAE-optimal
  comparator.

Under empirical-Bayes shrinkage the production path uses one positional mean per
position (`calibration_fit.py`); this module's thin/debut split matters only for
the legacy mode and the backtests.

### `calibration.py` — shrinkage k, affine correction, intervals

Post-processing for any point estimate
(`docs/research/scoring-engine-and-model-audit.md` §3-4).

**1. Variance components.** `variance_components` is a one-way random-effects
decomposition per position, keyed by (player, season) — pooling seasons would
book real level changes as noise and inflate k. A negative between-player estimate is clamped.
`icc` is also the R² ceiling for any model that knows only player identity.
Defaults (`DEFAULT_SHRINKAGE_K`), measured 2021-24:

| position | σ²_between | σ²_within | k | weight at n=6 |
|---|---|---|---|---|
| QB | 43.1 | 80.6 | 1.87 | 0.76 |
| RB | 30.3 | 33.6 | 1.11 | 0.84 |
| TE | 13.4 | 22.0 | 1.64 | 0.79 |
| WR | 27.4 | 35.6 | 1.30 | 0.82 |

- **DST k = 1.5**: unvalidated midpoint, never measured.
- **K k = 30** (2026-09-25): only 2-6% of a kicker's weekly variance belongs to
  the kicker (ICC .021-.056, against ~.4-.5 for skill positions). Inheriting
  ~1.5 let two PAT-only weeks drag Cameron Dicker (9.4 pts/game in 2025) to
  4.25. Leave-one-season-out 2021-25, weeks 2-18: RMSE is flat from ~20 to ~60;
  k=30 takes league-1 4.842 → 4.646 and league-2 4.879 → 4.679 (p≈1e-12) with
  pairwise accuracy unchanged at 0.524 — kicker ranking from kicker history is
  nearly a coin flip, so a large gap between two kickers' projections is not
  information. A constant, not a run-time fit: the estimate swings 17-46 by
  season and is degenerate in 2022. Shrinking toward each kicker's own prior
  season did not help.
- A fitted k is trusted only with `MIN_PLAYERS_FOR_VARIANCE` (30) player-seasons behind it; within-player variance needs `MIN_GAMES_FOR_VARIANCE` (4) games.

**2. Affine recalibration.** `fit_affine` least-squares `actual = a + b ·
projected` per position on prior seasons; returns None (identity) on too little
data or a slope outside `AFFINE_SLOPE_BOUNDS`. `apply_affine` clamps at zero.
With b > 0 it is rank-preserving within a position: it fixes magnitude and
cross-position (FLEX) comparability without touching start/sit order. The
empirical-Bayes weight is the principled estimator (and reorders by sample
size); the affine is a pure magnitude fix. They fix different failures.

**3. Intervals** (`IntervalModel`). Error spread grows with the projection
(Breusch-Pagan p 4e-06 to 2e-110) and the outcome is right-skewed (skew 1.4-1.6
for RB/WR/TE), so a normal band over-covers (0.86-0.87 at nominal 0.80).
Residuals are bucketed by projection size, quantiles taken per bucket, and the
whole conditional quantile curve is stored on a 19-point grid
(`QUANTILE_GRID`); reported quantiles are 10/90 (for WR the p5 error is −8.7 and
p95 +12.3). Interpolation is flat outside the observed range so a huge
projection doesn't get an unbounded band. `min_rows` is a parameter because the
week-1 model sees one week a season; holding it to the in-season 150-row bar
dropped QB and TE intervals entirely.

**The mixture.** `interval(projection, play_probability=p)` describes the actual
outcome — zero with probability 1−p, the conditional distribution otherwise:

```
Q(t) = 0                           if t <= 1 - p
     = Q_cond((t - (1 - p)) / p)   otherwise

floor   = 0                    if p <= 0.90 else Q_cond(1 - 0.90/p)
ceiling = Q_cond(1 - 0.10/p)   if p >  0.10 else 0
```

This replaced `lo * p, hi * p`, which held the floor away from zero while pulling
the ceiling toward it; its coverage at nominal 80% fell to 64% at p=0.85 and
26.5% at p=0.50, and 88% of the bands in the live week-1 report sat below
p=0.90, where the floor should be exactly zero
(`docs/research/second-audit-2026-09-02.md` §2). **Boundary trap:** `1.0 - 0.90`
is `0.09999999999999998`, so the comparison carries a 1e-9 tolerance. The floor
is clamped at zero for display.

### `calibration_fit.py` — fitting the calibration layer from history

The network adapter that gets real game logs into `calibration.py` and
`blend.py` and hands `generate_report.py` a bundle. Everything is fitted on
completed prior seasons only.

- **`load_game_logs`** computes league points per game and carries
  `blend.VOLUME_COLUMNS` through. With a scoring config it also runs
  `expected_td.enrich_game_logs` to add `exp_td_points`, `non_td_points` and
  `offense_pct`; those are not raw columns, so the generic copy loop skips them
  and the enrichment pass must run, or the blend silently fits with them
  permanently missing. A failed enrichment degrades to volume-only features.
- **`_walk_forward_rows`** produces, per player-week of a season, the projection
  that would have been made then plus the actual result. One baseline per
  position per week: empirical-Bayes shrinks at every sample size, so the old
  thin/debut split isn't needed.
- **`fit_from_history`** returns `{positional_baselines, shrinkage_ks, affines,
  interval_model, blend_model}`, each independently None-safe so one failure
  degrades one piece.
- **`dst_kicker_baselines`** — `POSITIONS` covers QB/RB/WR/TE only, so D/ST and
  K had no baseline. Invisible mid-season, but in week 1 of a new season the
  projection *is* the baseline, and every D/ST and kicker projected 0.00 in the
  first 2026 week-1 run. A prior-season positional mean is the honest fallback;
  `week1_kdst.py` then improves on it per entity.
- **`_load_context`** returns `{}` on failure; the blend then trains without
  game context.

### `blend.py` — volume and game-context refinement

A per-position ridge that refines the rolling average with rolling *volume* and
the week's Vegas context. Points are a noisy function of more stable inputs
(ICC, 2021-25):

| position | fantasy points | primary volume | target share |
|---|---|---|---|
| WR | 0.43 | targets 0.56 | 0.61 |
| RB | 0.46 | carries 0.60 | 0.44 |
| TE | 0.37 | targets 0.51 | 0.54 |
| QB | 0.25 | attempts 0.32 | — |

Touchdowns (receiving-TD ICC 0.07-0.11) are most of the noise. Held-out 2025
RMSE: QB 10.382 → 10.364 → 10.249, RB 6.676 → 6.630 → 6.585, WR 6.029 → 5.965 →
5.968, TE 5.430 → 5.351 → 5.337 (rolling only → +volume → +volume+context).
Modest (1.5-3%), and honestly so.

- **Snap share** (`offense_pct`, second audit): held-out 2025, league-1 6.5089 →
  6.4872 (p=1.4e-03), league-2 6.1877 → 6.1662 (p=1.2e-03), pairwise up in both.
- The TD/non-TD split is deliberately *not* a feature (see `projections.py`).
- Ridge alpha = 30: flat from 10 to 100 on held-out 2025.
  `MIN_TRAINING_ROWS` (400) ≈ two seasons; below it a position keeps its base
  estimate.
- Per-position because `carries` means workload for an RB and mobility for a QB,
  and scales differ threefold. Unfitted positions return the row's own
  `shrunk_avg` → `rolling_avg` → 0, so wiring the blend in can never make a
  position worse. Predictions use the same z-clip and output cap as `week1.py`.
- **`check_coverage` / `BlendFeatureMismatch`.** `predict_one` imputes a missing
  feature at its training median — right for one player, silently wrong for a
  whole pool. That happened (v22): the report's game-log loader never carried the
  volume columns, every feature was imputed, and Jaxon Smith-Njigba (34 pts/game)
  projected 6. Now a feature present on ≥90% (`TRAINED_COVERAGE`) of a position's
  in-season training rows must be present on ≥50% (`PREDICTED_COVERAGE`) of the
  rows being predicted (over at least `COVERAGE_MIN_ROWS`), or the run raises. Rows with no prior game this season are excluded
  from the check.
- **`rolling_volume`** re-implements the decayed window rather than reusing
  `usage.py` (which answers a different question, as a multiplier). A column
  absent for every game in the window stays absent, so the missing-indicator
  carries it instead of a fake zero.

### `context.py` — Vegas implied team totals

`implied_team_total = (total_line ± spread_line) / 2`, from
`load_schedules()`, available for every week including week 1. nflverse quotes
`spread_line` from the home team's perspective, positive for the favourite, so
home = (total + spread)/2. Getting that backwards produces plausible swapped
numbers, so it is defined once here and shared by `week1.py` and `blend.py`.
`game_context_by_team` flips the spread for the away team so positive always
means "favoured". Unpriced games fall back to a league-average total (~44.5,
~22.2 per team).

This is the right encoding of opponent strength: a defence-vs-position rate adds
~0.003 RMSE for QB and nothing elsewhere once the line is present (−0.000 in the
week-1 ablation). The line already prices this season's opponent. Held-out 2025
in-season gain: QB 10.364 → 10.249, RB 6.630 → 6.585, TE 5.351 → 5.337.

### `ridge.py` — shared linear algebra

Ridge, L2 logistic (IRLS), imputation and standardisation, hand-rolled: every
fit is a few dozen columns over at most a few thousand rows, and adding
scikit-learn to a "run a script" project is a bad trade. Extracted from
`week1.py` when `availability.py` and `blend.py` needed the same primitives.

- `num` is the NaN-aware gate everything relies on.
- `column_stats` gives a constant column sd 1.0, so it standardises to 0 and
  gets no weight instead of dividing by zero.
- `solve` is Gauss-Jordan with partial pivoting; a singular pivot is skipped
  (every caller adds a ridge penalty, so singular means "no information").
- `ridge` leaves the intercept unpenalised (penalising it biases the level);
  alpha > 0 also keeps collinear columns solvable.
- `logistic` floors the IRLS weight p(1−p): "Out" drives P(play) to ~0
  immediately and the unfloored Hessian goes singular. A step that fails to
  solve keeps the last good coefficients.
- `Z_CLIP` clamps standardised inputs so a novel value is "extreme", not
  "arbitrarily far" (see `week1.py`'s 175.8-point TE).

### `entity_prior.py` — per-player prior-season shrinkage targets (weeks 2+)

In week 2 a player's average is one game, so `n/(n+k)` puts most weight on the
shrinkage target — and that target was the same positional mean for everyone,
collapsing the in-house tier exactly when it has to rank a lineup. This module
gives each QB/RB/WR/TE a target equal to his own prior-season per-game mean,
itself shrunk toward the positional mean by games played, via the
`entity_baselines` hook. `project_player` is unchanged.

On the real 2026 week-2 report the symptom was in-house TEs at mean 4.19, sd
0.81. (A worse-looking version — TE sd 0.56, Goedert 126th of 209 — came from a
run without `FANTASYPROS_API_KEY`, which pushed consensus players into the
in-house tier; with the key Goedert is consensus-tier and ranks 3rd.) What the
fix demonstrably does to the shipped report is reduce ties: distinct in-house
values QB 74→92, RB 123→155, WR 162→215, TE 107→122. It does not uniformly widen
the spread.

Leave-one-season-out, weeks 2-9, bare estimator: RMSE down ~0.2 and pairwise up
~0.02 in all four folds (p < 1e-4). **Through the shipped stack** (with the
blend), the number to quote:

| | train → eval | RMSE | pairwise | Spearman |
|---|---|---|---|---|
| league-1 | 2024 → 2025 | 7.1540 → 7.1070 | .6903 → .6932 | .591 → .598 |
| league-1 | 2025 → 2024 | 7.0370 → 6.9900 | .7075 → .7103 | .629 → .636 |
| league-2 | 2024 → 2025 | 6.1140 → 6.0710 | .7461 → .7481 | .682 → .689 |
| league-2 | 2025 → 2024 | 6.2020 → 6.1610 | .7439 → .7443 | .687 → .691 |

Significant (p < 1e-3) in all four, never changes sign, concentrated in weeks
2-6. **One live week did not confirm it**: grading 2026 week 2 on players who
played, all QB/RB/WR/TE n=343 RMSE 6.749 → 6.733 (p=0.85), in-house n=297 RMSE
5.511 → 5.616 (p=0.22, wrong direction). Too small to overturn, not confirmation
either. **Trap:** grading the whole pool with non-players counted as 0 makes the
change look much better on RMSE and much worse on Spearman — that metric
measures attendance.

- No week cutoff: the edge is largest in week 2 and fades to noise by week 9,
  never negative. That decay *is* `n/(n+k)` self-attenuating.
- One global `DEFAULT_PRIOR_K = 2.0`. Per-position optima jumped up to 6×
  between adjacent seasons (QB 4.0 → 1.5, TE 0.5 → 3.0) — noise on a flat curve.
- Scope: QB/RB/WR/TE, weeks 2+ (`applies`). Week 1 has its own models; K and
  D/ST in weeks 2+ are unmeasured and not covered.
- No minimum-games filter: `shrink` already weights a one-game prior at 1/(1+k).
- A player with no usable positional baseline is omitted, so the caller falls
  back to the positional baseline.
- One prior season only (two was worse for `week1_kdst`; not re-tested here).
- Shares `shrink` with `week1_kdst.py` so the formula exists once.

### `expected_td.py` — the TD / non-TD split

`points = non-TD points + TD points`, and touchdowns are the least stable part.
`load_ff_opportunity()` (2021+) has expected TDs from opportunity. Replacing
*only* the TD term helps every position standalone (QB 10.114 → 10.080 p=0.55,
RB 6.529 → 6.468, WR 6.420 → 6.378, TE 5.336 → 5.276). Replacing expected points
*wholesale* is worth nothing (the blend already contains it). The estimator that
uses this is off — see `projections.py`.

- League-aware: expected TDs convert to points through each league's own config
  (league-1 passing TD 6 at the time, league-2 4), keeping `non_td + td ==
  total` exact.
- `split_game_row` always sets `non_td_points` and sets `exp_td_points` only
  when an opportunity row exists (~90% coverage); absent is not zero.
- `enrich_game_logs` also attaches `offense_pct` (snap share, which *is* used
  by the blend). Keys are written into game logs, which `compute_league_points`
  ignores because they aren't config categories. Renaming them requires the
  same rename in `blend.VOLUME_COLUMNS`.
- Adapters load **one season at a time**: nflverse has no file for a season
  with no games played, and a combined `[2025, 2026]` request raises and takes
  2025 down with it (the first 2026 week-1 run loaded zero rows for this
  reason). `load_snap_shares` joins PFR ids to gsis ids via
  `load_ff_playerids()`.

---

## 5. Week 1

### `week1.py` — cold-start model for QB/RB/WR/TE

`projections.py` never reaches into the prior season, so in week 1 everyone was
a flat positional mean: pairwise accuracy 0.50, MAE 5.96 (2022-25 week 1s). This
per-position ridge over pre-season information scores 0.711 and 4.81 — about
what the rolling average reaches by mid-season
(`docs/research/week1-cold-start-model.md`). Output matches
`project_player`'s contract with `source = "week1_model"`. Wired in only at
`--week 1`.

Feature blocks, by RMSE cost of dropping them (held-out 2024/25):

| block | ΔRMSE if dropped |
|---|---|
| Vegas game context | +0.169 |
| draft capital + rookie flag | +0.156 |
| prior-season production | +0.065 |
| age / experience | +0.060 |
| prior-season volume / role | +0.044 |
| team change | +0.017 |
| durability | +0.007 |
| depth chart | +0.000 (kept: its missing flag is informative) |
| opponent DvP | −0.000 |

A defence-vs-position rate from last season is worth nothing once the line is
in. `prior_season_dvp` (regressed 50% toward 1.0) is still computed so the
ablation is reproducible and the UI can show a matchup label.

- **Per-position** ridge: QB averages 15.2 pts/game and TE 5.5, and features
  mean different things by position.
- Missing features are median-imputed with a `_missing` indicator, so "unknown
  prior target share" is learnable. Prior-season fields stay None, not 0 —
  "didn't play" and "played and produced nothing" differ.
- Undrafted players get a fill pick at the far end of the scale (going undrafted
  is the signal).
- **Alpha = 120**, swept 25-2000 over 2021-25 week 1s; MAE/RMSE flat 25-250
  (4.818-4.834 / 6.340-6.385), 120 minimises miscalibration (mean |slope − 1|
  0.123 vs 0.170 at 25).
- **Extrapolation guards**: an undrafted 34-year-old TE standardised to a huge
  z-score and produced a 175.8-point projection, tripling that season's RMSE.
  Standardised features are clamped to ±`Z_CLIP`, and output is capped at the
  largest week-1 score seen for the position in training and floored at 0.
- `feature_coverage` on each output tells "lots of data" from "a rookie with a
  draft pick".
- **`fit_interval_model`** fits intervals on *walk-forward held-out* week-1
  residuals (in-sample residuals are over-confident; the in-season band is a
  different estimator). `min_seasons=1` because at 2 only ~700 rows were held out
  and QB/TE fell under the floor. Fewer, wider buckets.
- Training labels exist only for players who played week 1, so the output is
  conditional and gets the availability multiplier like everything else.
- `WEEK1_TRAIN_SEASONS = 4` (in `generate_report.py`): ~1 350 rows; each season
  needs two prior seasons for features.
- `_load_depth_ranks` handles both nflverse depth-chart schemas and filters
  snapshots to before week-1 kickoff; returns `{}` on failure.

### `week1_kdst.py` — kickers and defences in week 1

A positional mean in week 1 gave every kicker and every defence the same number,
leaving two of league-1's eight starting slots unrankable. Unlike a skill
player, last season is real information about a K or a D/ST (a defence *is* its
team). This is the prior-season per-entity mean shrunk toward the positional
mean by games played, with k fitted at run time.

Leave-one-season-out, 2020-2025 week 1s (2020 excluded: no crowds, distorted
lines), against the flat baseline:

| | RMSE | p | pairwise |
|---|---|---|---|
| league-1 K | 5.194 → 5.133 | 0.045 | n/a → 0.537 |
| league-1 D/ST | 6.702 → 6.391 | 0.014 | n/a → 0.566 |
| league-2 K | 5.162 → 5.068 | 0.031 | n/a → 0.546 |
| league-2 D/ST | 6.201 → 6.004 | 0.048 | n/a → 0.555 |

"n/a" literally: flat predictions tie and order no pair.

- k is grid-searched (coarse; the surface is flat). Fitted values land far apart
  (K ~10-20, D/ST ~3-4): a kicker's prior season says much less than a
  defence's. Defaults when history is thin are the fitted midpoints.
- `MIN_PRIOR_GAMES = 4`.
- **Not built, measured first**: a Vegas term (helped league-1 D/ST, hurt both
  kickers, flat for league-2 D/ST — a term that changes sign across leagues is
  not shippable), and a two-season prior (worse everywhere).
- Kicker logs are built exactly like the report's own loader (offence map ∪
  kicker map) so the scale matches. The positional mean comes from the same
  prior season as the estimates.
- `KDST_HISTORY_SEASONS = 6` (in `generate_report.py`) gives ~110 K and ~128
  D/ST pairs, above `MIN_FIT_OBSERVATIONS = 30`.

---

## 6. Availability

### `availability.py` — P(this player takes the field)

The highest-value module. Every estimator is fitted on played games, so its
number is "points if he plays", but the report presents an expected value. Over
2018-2025, 20.6% of weeks inside a player's active span are missed (QB 26.2%,
TE 23.9%, RB 18.5%, WR 18.4%) — different fractions for different players, which
breaks start/sit comparisons. A bare play rate is worth ~0.69 lineup pts/week
against ~0.15 for every estimator knob combined; with the injury report it is
+1.73 (p < 1e-240), because the most valuable fact — Out means P(play) = 0.0006
— is not in any history.

Empirical P(play), 2018-2025, 57 050 player-weeks:

| report status | P(play) | n |
|---|---|---|
| Out | 0.0006 | 1 540 |
| Doubtful | 0.0115 | 262 |
| Questionable | 0.6938 | 2 815 |
| not listed | 0.8271 | 52 431 |

Practice splits Questionable: DNP 0.511, Limited 0.702, Full 0.790.

**`AvailabilityModel`**: L2 logistic over report status, practice status, the
player's own play rate shrunk toward the league rate (`PLAY_RATE_PRIOR_GAMES`:
two observed weeks is not evidence of 50% availability), how much history there
is, position, and **position-by-depth indicators** plus a "rank unknown" flag
(~9% of player-weeks have no chart entry). Depth enters as interactions because
the effect isn't uniform or even monotonic: a backup QB plays 0.49, a backup RB
0.88. Without depth every undesignated QB got 0.76 whether he was the starter
(really 0.97) or third string. Held out on 2024-25 after fitting 2018-23
(14 991 player-weeks): log loss 0.374 vs 0.509 base rate, Brier 0.116 vs 0.164,
AUC 0.823 (0.854 with depth); worst decile calibration gap 0.068; Out predicts
0.007 vs 0.000 observed, Questionable 0.682 vs 0.701. With too few rows it falls
back to the shrunk rate; unfitted it returns the league rate, not 1.0.

- `ALWAYS_AVAILABLE_POSITIONS = {DST}`: a team defence always plays.
- **Kickers are in training** (2026-09-02). Excluded before, they fell on the
  reference level and predicted 0.367-0.831 against a measured play rate of
  0.932, the highest of any position.
- `play_rate_history` uses the *team's* played weeks as denominator so a bye is
  not a miss.
- `build_training_rows_nflreadpy` labels only weeks inside a player's active
  span (first to last game that season) where his team played; otherwise a
  week-10 signing counts as nine misses and the model fits roster churn.
  Depth ranks come from `depth_charts.py`.
- `_text` is NaN-safe (`value or ""` returns the NaN).

**Injury sources and merging.** nflreadpy's `load_injuries()` is the only source
with practice participation but caps at the last completed season. ESPN
(`espn_injuries.py`) covers ~449 of a 904-player pool, dated. Sleeper ~167,
keyed differently. IR, PUP and suspension collapse to "Out" (the calibrated
level; the answer is ~0 anyway). `load_current_injury_report` returns
`(report, source)` so a run says what it used.

`merge_injury_reports` merges **per player, never per source**. nflreadpy used to
win outright whenever it returned anything; mid-week it returns only teams that
have filed (22 rows, all ATL/GB, on 2026-09-23), so ESPN's 800 were discarded
and 96 of 96 Out/IR/Doubtful players went above P(play) 0.2 — Jayden Daniels
(Out) projected 13.8. Now ESPN and Sleeper designate everyone they list (ESPN wins
an overlap: it's dated), then an nflreadpy row overrides practice always and
game status only when it has one (a mid-week `None` must not erase an ESPN
"Out").

### `depth_charts.py` — depth rank as of a week, across both schemas

| feed | shape |
|---|---|
| through 2024 | one row per player per week; `depth_team` ranks **within a slot** (all three starting WRs are 1) |
| 2025 onward | timestamped league-wide snapshots (`dt`, `pos_rank`, `pos_slot`, `pos_abb`), no week column; `pos_rank` runs **across the position** (Chase 1, Higgins 2, Iosivas 3) |

- `rank_map_from_snapshots` takes the latest snapshot at or before each week's
  first kickoff. The feed extends past the season; unfiltered, a week-1
  projection got a chart from the following March.
- **`slot_ranks`** (v24) re-ranks `pos_rank` within (snapshot, team, grouping,
  slot), reproducing the legacy meaning (WR mix 66/32/2% vs legacy 63/33/4%; raw
  was 36/25/39%). Read raw, every starting WR2/WR3 looked like a backup, and
  healthy Tee Higgins projected P(play) 0.842 against a WR1's 0.955. Held out on
  2025: log loss 0.3217 → 0.3160, AUC 0.8742 → 0.8779 (WR 0.8487 → 0.8661). All
  32 teams line up 3WR 1TE, so only receivers were affected.
- Ranks are bucketed 1/2/3+ (`bucket_rank`): past third string the play rate
  flattens. Legacy rows keep the best rank when a player is listed twice.
- `load_depth_ranks` returns `{}` for an unavailable or unrecognised season.
- A feed can keep a column's name and change its meaning; nothing errors.

### `espn_injuries.py` — ESPN's league-wide injury report

The JSON behind espn.com/nfl/injuries: status, date and beat-reporter commentary
per player. Same unofficial-API family as the news endpoint.

| source | coverage (904-player 2026 pool) | carries |
|---|---|---|
| nflreadpy | 0 (last completed season only) | practice participation |
| Sleeper | 167 | designation + body part |
| ESPN | 449 | status + dated commentary |

It also feeds the news layer better than the general news endpoint (50
league-wide articles that name-match ~83 players).

- Requires a browser User-Agent: a descriptive one gets 403.
- `FETCH_FAILURES` records why a fetch failed, so a 403 doesn't look like
  "nobody is injured".
- Status map: Active (411 of 800) → no designation (listed for news, not doubt);
  IR (194), Out (37), Suspension (4) → "Out"; Questionable (154).
- `athlete.id` is null on every entry; the id is parsed from the profile URL,
  with a name fallback (435 by id + 14 by name on the 2026 pool). First row wins.
- `news_articles_from_injury` reshapes a row into `news.py`'s article shape.
- The ~9 MB payload is fetched once per run and shared.

---

## 7. The consensus tier

FantasyPros' free tier returns projected stat lines for the top 10 per position
only (hard cap, no pagination). Those players get the consensus tier; everyone
else gets the in-house estimate. Both vendors' stat lines are rescored through
our config; their points fields are never used.

### `fantasypros.py`

- One call per position, with a 2-second delay (the free tier rate-limits; a 429
  was hit in testing). `position=ALL` caps at 10 players *total*. `scoring` is
  not sent (ignored by the free tier).
- Pre-flagged milestone booleans and attempts are unmapped (we recompute
  milestones from yardage); `2pt_tds` can't be split by type; `ret_tds` isn't an
  individual category here. "fumbles" maps to `fumble_lost`.
- **`impute_unpublished_categories`** (2026-09-06). FantasyPros publishes
  yards/TDs/receptions/INTs/fumbles only, so under league-1's config a consensus
  line lost first downs, completions, incompletions and sacks: RB 3.29 and WR
  2.98 pts/game on 2025 actuals. Because the two tiers are ranked against each
  other, a bias in one makes them non-comparable — "absent contributes 0" is
  right for a rule a league doesn't have and wrong for one it does. Per-position
  least squares on 2022-25 from published fields only (R²/MAE: receiving first
  downs WR .842/.473, RB .702/.287; rushing first downs RB .763/.669;
  completions .780/3.00 and attempts .703/5.24 off passing yards). Sacks are a
  constant 2.13 (passing yards explain R² 0.043). Only fills absent keys; a no-op
  for leagues that don't score these. On the top-10 population, bias went QB
  −0.93 → +0.19, RB −3.66 → +0.18, TE −1.92 → −0.02, WR −5.47 → −2.94 (the rest
  is return yards nobody publishes).
- `position` is carried at the top level of a projection, not in the stat line,
  so blending can average stat lines numerically; the position is needed both to
  pick the imputation model and because league-1 prices TE receptions double.
- Identity is never re-derived from FantasyPros; the gsis id comes through the
  DynastyProcess crosswalk (`load_ff_playerids()`, `fantasypros_id` arrives as a
  float). An unresolved fpid (recent rookies) falls back to the in-house tier.

### `rotowire_projections.py`

Rotowire's weekly-projections endpoint: free, no key, top 10 per position, no
pagination. `offrecatt` is projected **receptions**, not targets (verified:
`ppr − fantasy == offrecatt` on every row). It publishes real completions and
attempts, so its incompletions are exact; first downs and sacks are imputed with
the FantasyPros estimator (which never overwrites a published value). Identity
by (normalised name, team), with a name-only fallback for mid-week trades.

**`blend_consensus_projections`** averages stat lines key by key for players
covered by both sources, then scores once (non-numeric keys are taken from
whichever side has them). The blended line must be scored with a position or
every blended TE loses half a point per catch. Each entry records
`contributing_sources` — what actually fed it — and `consensus_source_label`
turns that into "FantasyPros", "Rotowire" or "FantasyPros + Rotowire". A static
two-source label on the whole tier would claim agreement where one source had no
data. A failed position fetch returns an empty list for that position.

---

## 8. Built, measured, and left off

Each of these is tested and kept in the repo, but not wired in as a positive
default, because the measurement said no.

### `matchup.py` — opponent win-record multiplier

Bands the opponent's overall win percentage into a multiplier for
`project_player`'s `opponent_multiplier` (below .400 boosted … above .750
suppressed). Lower bounds inclusive: .400 → 1.00 band, .600 → 0.98, .750 → 0.95.
Uses last season's final record until the opponent has 4 games this season, then
switches outright (a hard cutover, not a blend). Overall record conflates offence
and defence — a known simplification. Backtests found no accuracy benefit and a
bias cost, and `context.py`'s Vegas line is the better encoding of opponent
strength. `win_pct` counts ties as half; unplayed games are skipped.

### `usage.py` — usage-trend multiplier

Recent usage share (`wopr` by default, `target_share` as comparator) over the
player's own trailing baseline, as a multiplier. A ratio needs no cross-player
calibration and targets role-emerging players.

Checked against live 2025 data before any constant was chosen: `wopr` can be
negative (min ~−9), so the ratio is clamped to a positive range *before* raising
it to `alpha` (a negative base to a fractional power is a domain error); the
ratio's real spread is wide (p10 0.45, p90 1.58), so output bounds come from it.
`baseline_usage <= MIN_BASELINE_USAGE` → neutral, which handles QBs (wopr ~0),
zero-target receivers and tiny denominators with one rule. RB scope is
receiving only. **Rejected**: pooled MAE not significant (tuning p=0.51,
holdout p=0.75); `DEFAULT_USAGE_ALPHA = 0.0` makes the multiplier exactly 1.0.
Usage columns ride through game logs as non-scoring keys, not via `scoring.py`
(whose mapper drops falsy values and would discard a real 0.0).

### `rotowire.py` — red-zone and route-efficiency annotations

Per-game Rotowire logs (undocumented endpoint, no auth; `opp` is required but
ignored) for two signals nflreadpy lacks: goal-line touches (`rzTargets5 +
rzRush5`) and targets per route run (`tprr`, stored as a percentage string,
converted to a fraction). Annotations in waiver rationale only, above
`MIN_RZ_TOUCHES_FOR_NOTE` / `MIN_TPRR_FOR_NOTE` (20%); they don't change points
(that would need its own backtest — `csv_backtest.py --rotowire` runs one).
Rotowire ids come from DynastyProcess (4 740 skill players, 0 gaps). Responses
are cached to `rotowire_cache_{season}.json` for 24 hours; a stale cache
re-fetches with a 0.5 s delay, and the fetch is skipped entirely before any game
has been played (see `should_skip_red_zone_fetch`). Everything degrades to
None/empty. D/ST is excluded (player-centric endpoint).

---

## 9. News

### `news.py` — news and injury flags

Produces the frontend's `newsFlag` shape — `{designation, riskLevel, summary}`
(`docs/specs/team-config-and-roster-status.md` §3.1) — from news text summarised
by Claude.

- **Sources.** ESPN's unofficial news endpoint (the only free narrative source;
  every fetch degrades to `[]`), ESPN injury commentary (preferred, per-player),
  and Sleeper's designations, which override the LLM's designation/risk level
  (more reliable than reading a news feed). Sleeper lookups are keyed by ESPN id
  and lowercase name; ids win on collision. A Sleeper body part becomes a minimal
  summary when there is none.
- **Matching** (`articles_for_player`): ESPN athlete id from the article's
  `categories` first, then a **full-name** match. Last-name matching triggered an
  LLM call for every Brown/Williams/Hill against unrelated articles. An empty
  name with no id matches nothing.
- **Validation.** `_validate_news_flag` coerces the reply to the closed enums;
  anything malformed becomes "no flag", never a wrong medical-sounding claim.
  `_parse_json_object` strips markdown fences and falls back to the outermost
  `{…}`: a bare `json.loads` on a fenced reply raised, the except swallowed it,
  and every summary in every report was empty until 2026-09-01.
- **`SUMMARY_FAILURES`** counts degrades, and the report prints the count.
- **Cache.** With a store and a player id, summaries are keyed on
  `(player_id, sha256(prompt))` — the prompt, because a prompt change must
  invalidate too. `SUMMARY_MODEL` is in the hash and also checked. Only real
  validated summaries are written; a failure is never cached. 167 of 936 players
  in a week-1 fixture carry a summary, so cache hits are most of a run's LLM
  cost.
- The client is duck-typed (`.messages.create`) so tests need no `anthropic`
  package; `build_anthropic_client` is the real factory.

---

## 10. Report generation

### `generate_report.py` — the orchestrator

Loads everything once, then per league: validates the config, builds the tiers,
applies availability, attaches intervals, selects waiver targets and writes the
fixtures. Script, not service — a deliberate decision (CLAUDE.md v12).

**Paths.** `--leagues-config` (multi-league, from `leagues.json`) and
`--scoring-config` (single league) are mutually exclusive. Both must go through
`_game_logs_for_config`; the single-league path used to skip it, so `offense_pct`
was missing at prediction time and `check_coverage` raised (v23).

**Tier precedence** in `build_weekly_report_and_pool`: consensus → week-1 model
(week 1 only) → in-house. Players on bye are skipped, not zero-projected. Every
optional input defaults to "behave exactly as before it existed".

- **Consensus tier** gets the availability multiplier (a vendor projection is
  conditional too) and an interval from the in-season residual model — an
  approximation, but most band width is irreducible outcome variance, and if the
  consensus is better the band is slightly too wide, the safe direction. It
  deliberately does **not** get the affine correction: that fits the in-house
  estimator's measured over-dispersion (slope 0.826), and we have no consensus
  history to fit theirs. Its `games_used`/`per_game_points` are placeholders;
  waiver cutoffs (≥14) exceed its depth, so consensus players are never waiver
  candidates.
- **Week-1 tier** carries its own held-out interval model so the mixture band
  can be derived once P(play) is known.
- **In-house tier**: k is resolved per player from the fitted map, otherwise by
  **position** (`calibration.DEFAULT_SHRINKAGE_K`). K and D/ST are never in the
  fitted map; falling through to the ~1.5 skill fallback dragged Dicker to 4.25.
  The blend refines the conditional number, availability is applied after, and
  the interval takes P(play) as an input (mixture), never `lo * p, hi * p`.
- **`entity_baselines`** merges `week1_kdst` (K/D/ST, week 1) and `entity_prior`
  (QB/RB/WR/TE, weeks 2+); disjoint by construction.

**Output shape.** `_projection_object`: `points` is expected, `conditionalPoints`
and `playProbability` appear only when availability was modelled (otherwise
they'd imply a distinction the run didn't draw). `floor`/`ceiling` are the
10th/90th percentiles, optional. A per-player `source` overrides the tier label
for the consensus tier. `_store_projection_rows` maps `points` → expected and
`conditional_points` → if-he-plays, explicitly.

**`_update_manifest`** tracks `weeks` (fixtures on disk), `latestWeek` (highest
fixture — 18 if the whole season was regenerated pre-kickoff), `currentWeek` (the
week the season is on, from `season_week.py`; what the UI should open on, omitted
when unknown) and `teamCount` (replacement level is per league).

**Loaders and degrades.**

- `load_player_pool_nflreadpy`: all 53-man rostered players (IR/inactive/PUP
  stay visible with their status), practice squad excluded, QB/RB/WR/TE/K by
  gsis id. **A missing gsis id arrives as NaN**; it is tested as a non-empty
  string. Duplicate rows from mid-season trades keep the first. The pool is the
  *current* roster snapshot, so pointed at a past season it silently drops
  players whose status has since changed.
- `load_all_game_logs_nflreadpy`: one `load_player_stats()` call, offence map ∪
  kicker map per row, regular season only (postseason weeks restart at 1). A
  season with no published file yet (404 before week 1) degrades to empty logs.
  **It must copy `blend.VOLUME_COLUMNS`**: it didn't, and every in-house
  projection collapsed toward a positional mean (v22).
- `load_dst_pool_and_game_logs_nflreadpy`: same cold-start contract for team
  stats; the D/ST pool still loads from the schedule.
- `validate_scoring_config` runs every band/TD-family validator before scoring.
- `build_consensus_tier_or_empty`, `build_news_client_or_none` (returns *why*
  — a missing `anthropic` package used to be reported as a missing key),
  `load_rotowire_stats_or_empty`: all degrade to empty.
- `load_news_flags_nflreadpy`: ESPN injury commentary first, general articles
  appended; Sleeper overrides designation; prints the failure count.
- **`load_env_file`** loads the repo-root `.env` (`override=False`, so CI's
  injected secrets win). Without it a local run silently skipped news and
  consensus: on 2026-09-20 Dallas Goedert projected 3.52 in-house instead of
  13.53 consensus. It reports which keys are live; a missing `python-dotenv`
  warns rather than raising (it isn't in `requirements-dev.txt`).
- **`should_skip_red_zone_fetch`**: skip when game logs are empty (season not
  started — the fetch is ~25 minutes of no-op) or when generating week 1
  (season-to-date aggregates would be lookahead). Was `week == 1`, which paid the
  no-op for all fifteen pre-kickoff regenerations of weeks 2-18.
- `main` sets `NFLREADPY_CACHE` to a filesystem cache by default: in-memory
  caching re-downloaded a dozen files every run, and nflverse's release assets
  start answering 404 under repeated traffic.
- Fetched once and shared: ESPN injuries (news + availability), raw consensus
  feeds (scored per league), expected TDs and snap shares, Rotowire stats.
  Calibration, week-1 K/D/ST and entity priors are fitted per scoring config and
  cached by `config_hash`, so two leagues sharing a config fit once.
- Availability: injury report via `load_current_injury_report`, current-season
  play rates with each team's weeks as denominator, depth ranks for the week
  (AUC 0.823 → 0.854; a QB1 moves 0.88 → 0.96 against an actual 0.98).
- Training windows: `AVAILABILITY_TRAIN_SEASONS = 6` (injury data starts 2018;
  limited by fetch time), `CALIBRATION_TRAIN_SEASONS = 3` (~35k player-weeks),
  `WEEK1_TRAIN_SEASONS = 4`, `KDST_HISTORY_SEASONS = 6`. 2020 is
  excluded from week-1 K/D/ST work.
- The store run is closed last and deliberately not in a `finally`: a run left
  `running` is a run that crashed.
- `LEAGUE_FORMAT_ASSUMPTION` is the single-league default; multi-league uses each
  league's `leagueFormat`. `TEAM_ABBR_DISPLAY_MAP` maps `LA` → `LAR`.

### `waiver_targets.py` — waiver selection and rationale

No ownership data exists, so "waiver-eligible" is a proxy: drop Out/Doubtful/IR,
then drop the top `DEFAULT_ROSTERED_RANK_CUTOFF[position]` players at each
position by projected points (assumed rostered in a 14-team league; generous for
bench and handcuffs; DST/K at 14 like QB/TE, since without a cutoff a real week
produced a waiver list of DST/K/DST). The multi-league path derives cutoffs from
`teamCount`. Candidates are ranked by **surplus over the last rostered player at
the same position**, not raw points — raw points surfaced nothing but backup QBs.
Top N overall, not per position. `describe_trend` needs two games (unknown is not
flat). `generate_rationale` is templated and deterministic, branching on
confidence and trend, with one Rotowire note appended (goal-line touches take
priority over tprr).

### `season_week.py` — which week to generate

`current_week_from_schedule` returns the earliest week with an unplayed game,
the last loaded week if the season is complete, or None. The scheduled workflows
call `generate_report.py` without `--week`, so this has to be right.
`_has_final_score` is NaN-aware: with `is None`, all 272 unplayed 2026 games read
as played and the function returned 18 a week before kickoff (v19). The CLI prints
only the week, for `week=$(python3 season_week.py --season 2026)`.

### `generate_team_config.py` — the "My Team" seed

Builds each league's `frontend/public/mock/<league>/default-team-config.json`
from `backend/leagues/<league>/roster.json`. That file is a list of player ids
indexed by position into `roster-slots.json`; maintained by hand until
2026-09-20, it kept seeding two dropped league-2 players for seventeen days.

The two files spell the same roster differently (league-2's roster says
`…,"K","DEF"` where slots say `…,"DST","K"`), so starters are placed by **slot
name** (`DEF`/`D/ST` aliased to `DST`), never by zipping. `check_slot_agreement`
compares the two as a multiset (order legitimately differs; a different count
means one is stale). `resolve_player_id` always consults the pool and narrows a
name match on position and team — a roster naming Isaiah Likely as BAL gets
"is in the pool as TE/NYG". `SLOT_ELIGIBILITY` mirrors the frontend's; every
placement is eligibility-checked. Everything raises (`TeamConfigError`), because
a half-working generator writes a plausible wrong fixture. `render` returns exact
bytes so `--check` (CI) diffs text. Usage:

```bash
python3 backend/generate_team_config.py
```

```bash
python3 backend/generate_team_config.py --league league-2
```

```bash
python3 backend/generate_team_config.py --check
```

---

## 11. Evaluation

### `track_record.py` — the live track record

Builds the frontend's `TrackRecord` from the weekly-report fixtures themselves —
each report *is* the prediction log entry for its week. Every `projections[]`
entry is a "start" prediction and every `waiverTargets[]` entry a "waiver_add".
A recommendation is correct if the actual reached `HIT_RATE_THRESHOLD` of the
projection (over-performing is always correct; a zero/negative projection is
correct on any production). These rules reproduce every value in the original
`track-record.json` mock. Unscored rows stay out of the summary, and summaries
default to 0/0.0, never null (the view calls `.toFixed` unconditionally). A
missing week contributes nothing.

### `generate_track_record.py` — grading CLI

Separate from `generate_report.py` because grading doesn't need to run every
time a report does. Grades only weeks strictly before `--as-of-week`; the current
week is included ungraded. Actuals come from the same bulk loaders as the
report, **with D/ST merged in** — without it every D/ST prediction stayed
ungraded forever, indistinguishable from "not played yet". A season with no D/ST
stats file degrades to no D/ST actuals. `--leagues-config` and `--reports-dir`
are mutually exclusive.

Note the store's reason for existing (§12): these fixtures are rewritten by the
pipeline, so a regenerated week replaces the prediction being graded.

### `backtest.py` — model-tuning harness

Grades projection *mechanics* on past seasons with a tuning/holdout week split
(weeks 5-14 tune, 15-18 confirm) over the full active pool (~600 players, one
`load_player_stats()` call). Results are only meaningful for comparing variants
against each other under the same config.

- **RMSE is primary, not MAE** (2026-09-01). Points are right-skewed (skew
  1.4-1.6, 14-22% of games ≤ 0); MAE is minimised by the median while a lineup
  maximises an expected total. Pooled mean bias ~0 against median bias +0.75 to
  +1.13. Every earlier round optimised the wrong loss.
- **`pairwise_accuracy`** (same position, same week; ties excluded) and
  **Spearman** (average ranks for ties) sit next to it. A variant can improve
  MAE while worsening the order — window=6 did.
- `paired_significance_test` is a paired z-test on per-pair differences
  (stdlib only, no scipy; rough below n≈30). `paired_variant_metric` restricts to
  rows graded by both variants (and optionally one confidence tier — only
  variant A's tier is consulted, fine while windows match).
- Errors are signed (positive = underprojected); squared error is carried for
  RMSE; median bias is reported next to mean bias.
- `backtest_player` skips weeks with no actual or no opponent (byes) but grades
  a week with no baseline using no shrinkage, so variants stay paired. It raises
  only when matchup/shrinkage is requested with no lookup at all (an empty dict
  is legitimate).
- `main` runs the window, decay, per-position window, shrinkage-strength (picked
  bias-first on the low tier), population (debut+thin vs all), usage-alpha and
  shrinkage+usage interaction sweeps, each checked on holdout and against the
  runner-up rather than trusting the table's ranking.

### `csv_backtest.py` — external-reference check

Compares in-house projections for all 18 weeks of 2025 against FantasyPros'
season CSV (standard PPR) and nflreadpy actuals. Players match by normalised name
+ team, then last name + team, then best token overlap. Empty cells (DNP) are
excluded; 0.0 and negatives are kept. Reports MAE/bias/correlation per position
and the L2-optimal scale `mean(actual) / mean(projection)`, with paired tests. QB
calibration uses nflreadpy actuals because the CSV's passing-TD value differs
from the league's; week 1 is excluded from the QB estimate (every QB is a no-data
row). `--rotowire` sweeps a goal-line-touch multiplier `1 + α·rz_score` (α ∈ {0,
.02, .05, .08, .10}) using the shared Rotowire cache and the same as-of rule.
This is where `POSITION_CALIBRATION_SCALE = 1.0` came from.

---

## 12. Storage

### `store.py` — cache and append-only prediction log

Implements phases 0-1 of `docs/design/shared-data-store.md`. The headline reason
is not caching: `generate_track_record.py` grades predictions read from fixtures
that `generate_report.py` overwrites, so regenerating week 5 in November silently
replaces October's prediction and grades today's model against October's
outcome. An append-only `projections` table is the fix.

| environment | backend | |
|---|---|---|
| `DATABASE_URL` | `PostgresStore` | shared, durable, append-only log |
| `FANTASY_STORE_DIR` | `FileStore` | content-addressed caches on disk |
| neither | `NullStore` | exactly the pre-store behaviour |

- Optional by construction, chosen only by `open_store` from the environment. An
  unreachable database falls back rather than failing a scheduled run.
- **Nothing raises into the pipeline**: a cache that can fail the run it speeds
  up is worse than none. `PostgresStore._exec` is the one place errors are
  swallowed.
- `content_hash` uses `sort_keys` (key order must not change the hash) and
  `default=str`. `CACHE_SCHEMA_VERSION` is in every hash; bump it when a cached
  artefact's meaning changes.
- **`config_hash`** keys model fits on the scoring config (underscore-prefixed
  provenance notes dropped), not the league, so two leagues sharing a config
  share a fit — the double-fit fix. `league_id` is stored for provenance only.
- `FileStore` caches only; it writes then renames so a killed run can't leave a
  truncated "hit". It deliberately has no prediction log — a laptop file can't be
  the append-only record.
- `PostgresStore.write_projections` is append-only: `on conflict do nothing`,
  never `do update`. `migrations/001_shared_data_store.sql` revokes UPDATE/DELETE
  on the pipeline's role. `psycopg` is imported lazily.
- `_resolve_cache_dir` resolves a relative `FANTASY_STORE_DIR` against the repo
  root, not the cwd (otherwise `backend/.store` becomes `backend/backend/.store`).
- `pack_bundle` / `unpack_bundle` serialise fitted `IntervalModel` and
  `BlendModel` via `__dict__` (floats, strings, containers only — verified on
  fitted instances). JSON turns tuples into lists; both classes only unpack and
  index them, so it's behaviour-preserving, but a future field that needs a real
  tuple would break silently. An unknown class name unpacks to None (refit).
- Fixtures stay the only thing the frontend reads; projections are written to
  the store alongside them.
