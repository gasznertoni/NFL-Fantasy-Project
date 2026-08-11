# Backend — Value Engine (early build)

Built while the real league scoring rules/roster are blocked on the
commissioner (`CLAUDE.md` Next Steps item 2). Covers items 4 ("design the
in-house projection model" — already existed as
`docs/design/in-house-projection-model-spec.md`, implemented here) and the
scoring-engine/news-layer portions of items 5 and 6, all built against a
placeholder config so nothing here needs to change shape once real values
land — only `scoring_config.placeholder.json` gets swapped for a real one.

## What's here

- **`scoring.py`** — `compute_league_points(stat_line, scoring_config)`.
  The one function both projection tiers (FantasyPros top-10/position and
  the in-house rolling-average tier) must go through, per CLAUDE.md's
  "never trust a vendor's precomputed points field" rule. Config-driven:
  linear categories, yardage-milestone bonuses, and DST points/yards-allowed
  tiers are all data, not hardcoded logic, so the 4 still-open ESPN schema
  questions (Next Steps item 3) are a config change whichever way they
  resolve.
- **`scoring_config.placeholder.json`** — a placeholder, NOT the real
  league's values. `pass_td: 6` is the one confirmed-real number
  (CLAUDE.md); `reception: 0.5` deliberately matches the frontend's
  existing half-PPR placeholder assumption so the two placeholders don't
  disagree with each other. Everything else is a reasonable ESPN-default
  guess. Swap this file once the commissioner responds.
- **`projections.py`** — the in-house rolling-average model per
  `docs/design/in-house-projection-model-spec.md` §3.2–3.3 and §5's output
  contract (`games_used`, `confidence`, `opponent_multiplier`). Also
  supports an exponentially-decayed average via `decay` (added 2026-08-10,
  `_rolling_average()` / `project_player(..., decay=...)`) — `decay=1.0`
  is the default and is arithmetically identical to the original unweighted
  mean, not just close to it. This was backtested (see the findings doc
  below) and found to make MAE significantly *worse*, not better, so
  `DEFAULT_DECAY = 1.0` stays the shipped default; the parameter exists so
  that finding is falsifiable against more data later without rebuilding
  it. Also supports thin-sample shrinkage toward a positional baseline
  (added 2026-08-11, Round 4) via `positional_baseline`/`shrinkage_strength`
  — `positional_baseline=None` (the default) is a no-op, arithmetically
  identical to omitting shrinkage entirely; a caller (`baseline.py`) injects
  a real value. `DEFAULT_SHRINKAGE_STRENGTH = 0.5` per the findings doc's
  Round 4 verdict (see `baseline.py`'s bullet below for the two confounds
  that had to be fixed before that number meant anything).
- **`matchup.py`** — opponent-strength adjustment feeding
  `projections.py`'s `opponent_multiplier` parameter. Deliberately simpler
  than the design spec's original §3.4 idea (defense-vs-position
  points-allowed): bands the *opponent's overall win-loss record* into a
  multiplier (below .400 → 1.08 boost … above .750 → 0.95 suppression, an
  explicit design choice from 2026-08-06, not a placeholder awaiting real
  values). Uses last season's final record until the opponent has played 4
  games this season, then switches to this season's to-date record. Known
  tradeoff, stated in the module docstring: overall record conflates
  offense and defense, so a great-offense/bad-defense team will read as
  "tough" here even though its defense specifically might be easy to score
  on — a real simplification versus a position-specific model, not an
  oversight.
- **`baseline.py`** (added 2026-08-11, Round 4) — positional-baseline
  computation for `projections.py`'s thin-sample shrinkage, mirroring
  `matchup.py`'s "compute externally, inject as a plain float" pattern.
  Two separately-scoped populations, resolved per player-week by
  `baselines_by_player_week_for_shrinkage`: `population="debut"` (a
  player's literal first game of the season, excluding week 1 via
  `min_week=DEFAULT_DEBUT_MIN_WEEK` — see the module docstring for why
  week 1 alone is a different population from a genuine in-season
  call-up) feeds the `no_data` tier; `population="thin"` (fewer than
  `DEFAULT_WINDOW` prior games) feeds the `low` tier. Both fixes were
  found empirically, not designed in from the start — the module docstring
  documents two real backtest confounds and the exact numbers that exposed
  them, worth reading before changing this module.
- **`news.py`** — News & injury layer (v1 scope item 2). Fetches ESPN's
  unofficial news endpoint, matches articles to a player (by ESPN athlete
  ID when the response's `categories` field has it, else a crude
  name/last-name substring fallback), and summarizes via an LLM call into
  the **exact `newsFlag` shape the frontend already expects**
  (`docs/specs/team-config-and-roster-status.md` §3.1) — `designation` /
  `riskLevel` / `summary` — so wiring this in later touches zero frontend
  components.
- **`backtest.py`** — historical backtest / model-tuning harness against
  real 2025 results. Grades the projection model week by week (projected
  vs. actual, both computed through the same placeholder scoring config
  so the comparison is apples-to-apples), reports MAE/bias/correlation
  broken down by confidence tier, and runs a window-size sweep plus a
  matchup-multiplier on/off ablation with a tuning-weeks/holdout-weeks
  split to guard against overfitting the choice to one season's noise.
  Also runs a **paired significance test** (`paired_significance_test()` +
  `paired_variant_metric()`, added 2026-08-10) on the matchup ablation's
  MAE and bias — a proper paired z-test on matched per-(player, week)
  values, replacing an earlier version of this doc's "eyeballed" call that
  a small MAE delta was noise. Plain-stdlib normal approximation rather
  than scipy's exact paired t-test (consistent with `_pearson_correlation`
  avoiding `statistics.correlation` for the same 3.9-venv-compatibility
  reason) — accurate enough at this backtest's sample sizes (n~155/65),
  rougher below n≈30. Also (added 2026-08-10) sweeps candidate `decay`
  values against `projections.py`'s new recency-weighting option, and
  segments the player pool by position for a position-specific window
  sweep — both findings doc idea #1 and idea #4, each checked against
  `DEFAULT_DECAY`/`DEFAULT_WINDOW` with the same paired significance test
  used for the matchup ablation, not just an eyeballed MAE table. Full
  methodology and honest caveats (this validates model *mechanics*, not
  real-2026 point accuracy) are in the module docstring — read that before
  trusting its output. Loads the full active QB/RB/WR/TE pool (no
  hand-picked player list) and must run locally (network + nflreadpy) —
  confirmed actually working end-to-end from this
  machine's `backend/.venv` on 2026-08-10 (unlike the Cowork cloud sandbox
  the rest of this backend was built in, where PyPI and nflverse's data
  host were both unreachable).
- **`tests/`** — 128 unit tests, all passing, covering the logic above with
  synthetic data (no network, no API keys needed to run these).
- **`docs/research/projection-model-backtest-findings.md`** — write-up of
  the backtest results across three rounds. Round 1 (19 hand-picked
  players): window=4 confirmed as the right default, the win-record
  matchup multiplier showed no benefit, plus a prioritized list of accuracy
  ideas for later rounds (recency weighting, shrinkage for thin samples,
  usage/opportunity features, position-specific tuning, a real
  position-specific matchup model). Round 2 (same 19 players): tested two
  of those ideas and rejected both — recency-weighted decay (MAE
  significantly worse as decay drops, p=0.0105) and position-specific
  window tuning (every position's apparent best window within noise of the
  pooled default, p≥0.20). **Round 3 (2026-08-11, full active pool — 611
  players, not hand-picked)** overturned or re-scoped several Round 1/2
  calls now that the sample is large enough to test them properly:
  window=6 now significantly beats window=4 on both tuning and holdout
  weeks (applied as `projections.py`'s new `DEFAULT_WINDOW`); RB and TE individually
  confirm a significant window=6 preference (Round 2's "no position needs
  an override" was underpowered at 3-9 players/position); Round 2's decay
  rejection doesn't replicate at full-pool scale (no longer significant —
  looks like a small-sample artifact, though the conclusion not to ship it
  is unchanged); and the `by_confidence` tier breakdown shows the
  `no_data` tier (zero games logged) is a literal predict-zero with no
  fallback, a sharper and more actionable version of the "shrinkage" idea
  than originally scoped. **Round 4 (2026-08-11)** built that shrinkage
  idea (`baseline.py`) and shipped it — but only after two real population
  confounds were found and fixed by actually running it against the full
  pool (a pooled "thin" population overshot the `no_data` tier badly, and
  even a properly-scoped "debut" population still overshot until week 1 —
  when the whole league's roster debuts at once — was excluded from it).
  Both fixes and the final bias-reduction numbers (`no_data`/`low` tier
  bias cut ~70-86% on both tuning and holdout weeks, small MAE guardrail
  cost) are in the findings doc's Round 4 section. All four rounds' code is
  in `projections.py`/`baseline.py`/`backtest.py`; Round 3's window=6 and
  Round 4's shrinkage-strength=0.5 findings are both applied as shipped
  defaults (`projections.py`'s `DEFAULT_WINDOW`/`DEFAULT_SHRINKAGE_STRENGTH`).

## What's deliberately NOT done here

- **DST scoring** — `scoring.py`'s tier/linear mechanism can express it,
  but assembling a real DST stat line needs the self-join/schedule-join
  logic already documented in `docs/research/dst-scoring-fields.md`
  (points-allowed, yards-allowed, block-credit). Not built yet — next
  logical chunk of item 5.
- **`matchup.py` isn't wired into `projections.py` yet** — `compute_opponent_multiplier()`
  produces a number in the exact shape `project_player()`'s
  `opponent_multiplier` parameter expects, but nothing calls the former to
  populate the latter yet; that's glue code for whatever orchestration
  layer eventually runs a full weekly report.
- **Real ESPN/nflreadpy network calls untested** — this was built inside
  the Cowork cloud sandbox, which could not reach either PyPI, GitHub's
  release-asset host (nflverse's data host), or ESPN's news endpoint
  (all confirmed blocked/unreachable during this build — same restriction
  `scripts/probe_sources.py`'s header already flagged for nflreadpy).
  `projections.load_recent_games_nflreadpy()` and `news.fetch_espn_news()`
  are therefore untested against live data — **run them locally** (where
  `scripts/probe_sources.py` already runs today) before trusting the
  adapter/endpoint shape assumptions baked into them.
- **No orchestration/API layer yet** — no FastAPI/Flask endpoint wiring
  this to the frontend's `lib/api.js` yet. These are still standalone,
  independently-tested modules — wiring them into a real service and
  swapping the frontend's mock fixtures for live calls is a distinct next
  step, not started here.

## Run tests

```bash
cd backend
python3 tests/test_scoring.py -v
python3 tests/test_projections.py -v
python3 tests/test_news.py -v
python3 tests/test_matchup.py -v
python3 tests/test_backtest.py -v
```

(9 of `test_backtest.py`'s cases cover the paired significance test —
noise/signal/degenerate-input cases and the metric-pairing logic,
including the asymmetric-skip edge case where one variant grades a week
the other doesn't.)

No dependencies needed for the tests above (stdlib `unittest` only,
deliberately — `pytest` wasn't installable from the cloud sandbox this was
built in either, and `backtest.py`'s correlation helper is hand-rolled
rather than `statistics.correlation` for the same reason it avoids that
stdlib function: `scripts/.venv` is Python 3.9, and that function needs
3.10+). Real runs (`news.fetch_espn_news`,
`projections.load_recent_games_nflreadpy`, `backtest.py`) need
`pip install -r requirements.txt` and, for the LLM summarization step,
`ANTHROPIC_API_KEY` set in the environment.

## Running the backtest

1. `pip install -r requirements.txt` locally (this needs network + PyPI,
   confirmed unavailable from the Cowork cloud sandbox this repo section
   was built in).
2. Optionally edit `SEASON` / `POSITIONS` near the bottom of `backtest.py`
   — `main()` loads the full active pool for those positions via
   `load_full_pool_game_logs()` (every player `nflreadpy` has a stat line
   for that season, ~600 for the default QB/RB/WR/TE), not a hand-picked
   player list.
3. `python3 backtest.py` from inside `backend/`. It prints, in order: a
   window-size sweep on tuning weeks, the same sweep on holdout weeks
   (only trust a window size that looks good on both), a
   matchup-multiplier on/off ablation plus its paired significance test,
   a decay-value sweep on tuning and holdout weeks plus its significance
   test against `decay=1.0`, and a position-specific window sweep (each
   position's apparent best window checked for significance against
   `DEFAULT_WINDOW`, same as the matchup/decay checks) — every "does X
   actually help" question in the codebase gets an actual p-value, not an
   eyeballed table.
