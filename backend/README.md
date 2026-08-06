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
- **`tests/`** — 65 unit tests, all passing, covering the logic above with
  synthetic data (no network, no API keys needed to run these).

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
