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
  contract (`games_used`, `confidence`, `opponent_multiplier`).
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
  Full methodology and honest caveats (this validates model *mechanics*,
  not real-2026 point accuracy) are in its module docstring — read that
  before trusting its output. Needs `PLAYER_IDS` filled in and must run
  locally (network + nflreadpy).
- **`waiver_targets.py`** — waiver-wire candidate selection + a
  deterministic, templated (no LLM) `rationale` sentence, per
  `docs/design/backend-frontend-integration-plan.md`'s gap #3. No real
  roster-ownership-% source exists anywhere in this project, so
  "waiver-eligible" is an in-house proxy: drop the top N ranked players
  per position (assumed already rostered in a 14-team league), rank what's
  left by **points above replacement** (not raw points — this league's
  6-point passing TDs put QB's raw scale well above every other position,
  which surfaced nothing but backup QBs when first tested against real
  2025 data; ranking on the surplus over the last-rostered player at the
  same position fixed that).
- **`track_record.py`** — live, in-season "how has the tool done so far"
  aggregation (`getTrackRecord()`'s shape), per gap #4. Distinct from
  `backtest.py`: this reads the tool's own past `weekly-report-week-N.json`
  snapshots back in as the prediction log (CLAUDE.md's eval-layer ask —
  "recommendations should be logged in a way that makes later accuracy
  scoring straightforward" — is satisfied by the reports themselves, no
  separate database needed) and grades them against real results.
  `HIT_RATE_THRESHOLD = 0.75` (a "start"/"waiver_add" call is correct if
  the actual outcome met at least 75% of the projected points) was checked
  to exactly reproduce every value — individual `outcomeCorrect` flags and
  the aggregated `predictionsScored`/`startSitHitRate`/`meanAbsoluteError`
  — in the existing `frontend/public/mock/track-record.json` mock fixture
  before being written here.
- **`generate_report.py`** — orchestration script tying `scoring.py` +
  `projections.py` + `news.py` + `waiver_targets.py` together into
  `weekly-report-week-N.json` and `player-pool.json`, in the exact shape
  `frontend/src/lib/api.js` already expects. Implements the integration
  plan's v0 build order: **real `nflreadpy` player IDs (`gsis_id`)
  directly as `playerId`**, no invented `p_00123`-style scheme and no
  permanent crosswalk (the plan's recommended resolution to its own
  player-ID open question — no real users/saved data existed yet to
  migrate), and **every player in the `in_house_estimate` tier** (an
  actual FantasyPros pull + top-10/position tier selection is gap #2,
  deliberately deferred by the plan itself, not a pending decision this
  script is blocked on). Only QB/RB/WR/TE are covered — `scoring.py`'s
  nflreadpy column map has no stat-line assembly for DST (needs the
  self-join work in `docs/research/dst-scoring-fields.md`, out of scope
  for this plan) or K (no column mapping exists for kickers yet either).
  Confirmed working end-to-end against real 2025 data (see "Generating a
  real report" below) and against the real, current 2026 season, where it
  correctly degrades to an all-cold-start report (nflverse hasn't
  published a 2026 stats file yet — no games have been played) instead of
  crashing.
- **`generate_track_record.py`** — separate CLI (can run on its own
  cadence, per the plan) that reads back whatever `weekly-report-week-N.json`
  files already exist and real actual results, and writes
  `track-record.json` via `track_record.py`.
- **`tests/`** — 129 unit tests, all passing, covering the logic above
  with synthetic data (no network, no API keys needed to run these).

## What's deliberately NOT done here

- **DST scoring** — `scoring.py`'s tier/linear mechanism can express it,
  but assembling a real DST stat line needs the self-join/schedule-join
  logic already documented in `docs/research/dst-scoring-fields.md`
  (points-allowed, yards-allowed, block-credit). Not built yet — next
  logical chunk of item 5. `generate_report.py` excludes DST (and K, same
  underlying gap) from the generated pool entirely rather than emit a
  silently-wrong zero.
- **`matchup.py` isn't wired into `projections.py` (or `generate_report.py`)
  yet** — `compute_opponent_multiplier()` produces a number in the exact
  shape `project_player()`'s `opponent_multiplier` parameter expects, but
  nothing calls the former to populate the latter. Per
  `docs/design/backend-frontend-integration-plan.md`'s "explicitly out of
  scope" section, this is deliberate: the backtest found no accuracy
  benefit and a real bias cost from enabling it, so it stays built,
  tested, and unwired.
- **The FantasyPros tier** — `generate_report.py` ships 100%
  `in_house_estimate` for now (gap #2 in the integration plan); a real
  FantasyPros pull and top-10-per-position tier-selection logic is future
  work, not started here.
- **No live API/serverless layer** — `generate_report.py` is a script you
  run before a deploy, not a service the deployed frontend calls at
  request time. Deliberate for now, per the integration plan's own
  architectural recommendation (a live endpoint adds hosting/CORS/secret-
  management work this portfolio project doesn't need yet) — revisit once
  the pipeline below has been run for real a few times.

## Run tests

```bash
cd backend
python3 tests/test_scoring.py -v
python3 tests/test_projections.py -v
python3 tests/test_news.py -v
python3 tests/test_matchup.py -v
python3 tests/test_backtest.py -v
python3 tests/test_waiver_targets.py -v
python3 tests/test_track_record.py -v
python3 tests/test_generate_report.py -v
```

No dependencies needed for the tests above (stdlib `unittest` only,
deliberately — `pytest` wasn't installable from the cloud sandbox this was
built in either, and `backtest.py`'s correlation helper is hand-rolled
rather than `statistics.correlation` for the same reason it avoids that
stdlib function: `scripts/.venv` is Python 3.9, and that function needs
3.10+). Real runs (`news.fetch_espn_news`,
`projections.load_recent_games_nflreadpy`, `backtest.py`,
`generate_report.py`, `generate_track_record.py`) need
`pip install -r requirements.txt` and, for the LLM summarization step,
`ANTHROPIC_API_KEY` set in the environment (omitted, or `--skip-news`
passed to `generate_report.py`, both degrade to the default healthy
`newsFlag` for everyone rather than failing).

## Generating a real report

```bash
cd backend
pip install -r requirements.txt   # needs network + PyPI

# One week's weekly-report-week-N.json + the full player-pool.json:
python3 generate_report.py --season 2026 --week 1

# Then, once at least one week is in the past:
python3 generate_track_record.py --season 2026 --as-of-week 2
```

Both default to writing into `frontend/public/mock/` (the exact path
`api.js` already fetches from — no frontend code changes needed to pick up
real output). Pass `--out-dir`/`--reports-dir` to write elsewhere instead
(useful for a dry run before overwriting the shipped fixtures). Confirmed
working end-to-end against real 2025 data (`--season 2025 --week 10`,
`--skip-news`: 438 active QB/RB/WR/TE players loaded, 383 projected that
week, 3 waiver targets selected with real templated rationale text) and
against real, current 2026 data (`--season 2026 --week 1`: correctly
produces an all-`no_data`/0.0-point report rather than crashing or
fabricating a number, since nflverse hasn't published any 2026 game stats
yet — the season hasn't started). The shipped `frontend/public/mock/*.json`
fixtures were deliberately left as the hand-authored demo data rather than
overwritten with that degenerate pre-season output — regenerate them for
real once actual 2026 games have been played.

## Running the backtest

1. `pip install -r requirements.txt` locally (this needs network + PyPI,
   confirmed unavailable from the Cowork cloud sandbox this repo section
   was built in).
2. Get real `nflreadpy` player IDs for the players you want to test —
   `db_playerids.csv` (already used elsewhere in this project, see
   CLAUDE.md's Data Sources table) has the crosswalk, or filter
   `nflreadpy.load_rosters()`'s output by name.
3. Edit `PLAYER_IDS` near the bottom of `backtest.py` with those IDs — aim
   for a mixed set of archetypes per the module docstring, not just top
   players.
4. `python3 backtest.py` from inside `backend/`. It prints three reports:
   a window-size sweep on the tuning weeks, the same sweep on the holdout
   weeks (only trust a window size that looks good on both), and a
   matchup-multiplier on/off ablation — does `matchup.py`'s win-record
   adjustment actually lower error, or just add noise?
