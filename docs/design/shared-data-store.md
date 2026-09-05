# Shared Data Store — Design (2026-09-02)

Status: planning only, nothing in this document has been built yet. Written
in response to the question "can we have a player database with scores,
projections and injury reports, so the Vercel app doesn't have to cache
everything on one computer only?"

The short answer is yes, and it's worth doing — but not primarily for the
reason the question implies. The serving side is already fine. What is
laptop-bound is everything the pipeline *consumes*, and the most valuable
thing a store fixes is a correctness bug in the eval layer, not an ops
inconvenience. This document works through that, proposes a schema, and
phases the build so the cheapest half can ship without any infrastructure
at all.

## What's already true (no decision needed here)

- **Vercel is not the bottleneck.** `frontend/public/mock/league-N/*.json`
  are static files served from a CDN. `frontend/src/lib/api.js` fetches
  them with plain `fetch()` and degrades to an empty state on anything
  that isn't JSON. Nothing in this document changes that, and nothing
  should: a 200–500 KB precomputed weekly report on a CDN is faster than
  any database read could be.
- **The script-based decision (2026-08-16) stands.** `backend/README.md`'s
  "No live API/serverless layer" bullet records it as decided, not
  deferred. What was rejected there was *recomputing at request time* —
  behind a rate-limited FantasyPros call and per-player LLM
  summarization — on the grounds that it would need a precompute/caching
  layer in front of it anyway, converging back to "compute on a schedule,
  serve the cached result."

  **A shared store is that caching layer, not a repudiation of it.** Phases
  0–2 below keep the pipeline exactly as script-shaped as it is today; they
  only stop the cache from living in one `~/Desktop` folder. Phase 3 is the
  only part that genuinely reopens the live-endpoint question, which is why
  it is separate, optional, and argued on its own merits.
- **`.github/workflows/weekly-report.yml` already runs the pipeline on a
  schedule.** The automation gap is closed. What is *not* closed is that
  the runner it schedules starts cold every single time.

## The measured problem

Where each piece of data lives today, and whether it survives leaving this
machine:

| Data | Where it lives | Survives a cold CI runner? |
|---|---|---|
| Rotowire pool game logs | `backend/rotowire_cache_2025.json` — 13 MB, **gitignored**, 597 players, 24 h TTL | **No** |
| nflreadpy season pulls | library-level cache, per-machine | **No** |
| LLM news summaries | **nowhere** — recomputed in full every run | **No** (never cached at all) |
| Fitted models (availability, calibration, blend, intervals, week-1 ridge) | recomputed every run, **per league** | **No** |
| Weekly reports, player pool | `frontend/public/mock/league-N/`, committed, ~5.4 MB per league | Yes (git) |
| Track record | `track-record.json` — 2 MB, 5 685 history rows, whole-file rewrite | Yes (git) |

`weekly-report.yml` has no `actions/cache` step, so every scheduled Tuesday
run pays full price for all four of the top rows:

- **Rotowire:** 597 players at `FETCH_DELAY_SECS = 0.5` is ~5 minutes of
  deliberate sleep alone, before response time, for data that changes once
  a week.
- **News:** 167 of the 936 players in the week-1 league-1 fixture carry an
  LLM-written summary, i.e. 167 `claude-haiku-4-5` calls
  (`news.py:287`), repeated in full on every run even when the underlying
  ESPN article is byte-identical to last time.
- **Availability:** refit over `AVAILABILITY_TRAIN_SEASONS = 6` seasons of
  injury history.
- **Calibration / blend / week-1 ridge:** `_build_calibration_for_config`
  and `_build_week1_for_config` are called *inside* the per-league loop
  (`generate_report.py:1400`, `:1404`), so with two leagues in
  `leagues.json` every fit runs twice — `fit_from_history` pulling
  `CALIBRATION_TRAIN_SEASONS = 3` seasons plus a lead-in year of game
  logs each time.

None of this is expensive because the computation is hard. It is expensive
because nothing that was computed is ever addressable by anything other
than the filesystem that produced it.

## The real reason to do this

`backend/generate_track_record.py:26` sources the prediction log from
`frontend/public/mock/league-1/weekly-report-week-N.json` — the same
committed fixture files `generate_report.py` overwrites.

**The eval layer's ground truth is a mutable file the pipeline rewrites.**

Regenerate week 5 in November and the record of what the tool predicted in
October is silently replaced by what today's model says. The track-record
view then grades the current model against past outcomes and reports the
result as a season-long accuracy figure. The git history already shows the
mechanism firing twice for week 1 (`bb8d284` "Regenerate week-1 fixtures
with real news summaries", `1a1c6b2` "Regenerate week-1 fixtures from live
2026 data") — harmless pre-season, but only because nothing had been played
yet.

For a project whose stated differentiator is *"track the tool's own
recommendation accuracy against actual results"* (CLAUDE.md, v1 scope item
4), a prediction log that rewrites itself is a correctness defect, not a
storage-efficiency question. An append-only `runs` + `projections` pair
fixes it properly, and file-shaped storage fundamentally cannot: the
fixture path has no room for a second version of week 5.

This is the argument that carries the whole document. The caching wins
below are real but secondary.

## Recommendation

**Postgres on Neon, as the pipeline's canonical store. The frontend keeps
reading static JSON exported from it.**

Sizing, so the tier choice isn't a guess: a full season is ~936 players ×
18 weeks ≈ 17 k player-weeks; projections at two leagues and one run per
week ≈ 34 k rows a season, and the ten-season nflreadpy history the audit
used is 56 554 player-games (CLAUDE.md, v16). The entire dataset, history
included, sits comfortably inside a free tier with two orders of magnitude
to spare. This is not a scale decision — it is a *shape* decision. What
Postgres buys is addressability (a cache key that isn't a file path),
append-only history, and a join between predictions and outcomes.

Why Postgres over the alternatives considered:

- **Object storage + Parquet (R2/S3 + DuckDB).** Cheaper, and a better fit
  for `backtest.py`'s bulk scans. Rejected as the primary store because the
  append-only prediction log wants row-level writes and a uniqueness
  constraint, which is exactly what object storage is worst at. Worth
  revisiting later as a *secondary* export for backtesting if scan
  performance ever matters.
- **SQLite committed to the repo.** Zero infrastructure, works offline.
  Rejected: a binary file rewritten weekly by CI is worse in git than the
  JSON it replaces, and it reintroduces the "one copy, one machine"
  problem the moment two runners touch it.
- **`actions/cache` in the workflow, nothing else.** Genuinely fixes the
  cold-runner cost for ~20 lines of YAML. Rejected as *sufficient* because
  it does nothing for the prediction-log defect above, and GitHub evicts
  caches after 7 days of no reads — a weekly-cadence pipeline sits right on
  that boundary. Still worth doing as a stopgap if Phase 1 slips.

## Schema

Grain is stated for every table, because the silent-column-map lesson from
v16 applies here too: a join at the wrong grain produces a plausible wrong
number rather than an error.

```sql
-- Provenance. One row per generate_report.py invocation.
runs (
  run_id        uuid primary key,
  season        int not null,
  week          int not null,
  git_sha       text,
  config_hash   text,          -- hash of the scoring config + model params
  started_at    timestamptz not null,
  finished_at   timestamptz,
  status        text            -- running | ok | failed
)

-- Slowly-changing player identity. Grain: one row per player.
players (
  player_id     text primary key,   -- nflreadpy gsis_id, the existing scheme
  name          text not null,
  position      text not null,
  team          text,
  espn_id       text,
  updated_at    timestamptz not null
)

-- Shared upstream cache. Grain: (player, season, week, source).
player_games (
  player_id     text references players,
  season        int, week int,
  source        text,           -- nflreadpy | rotowire
  stat_line     jsonb not null,
  fetched_at    timestamptz not null,
  primary key (player_id, season, week, source)
)

-- Grain: (player, report date, source).
injury_reports (
  player_id     text references players,
  report_date   date,
  source        text,           -- espn | sleeper | nflreadpy
  designation   text,
  practice_status text,
  raw           jsonb,
  primary key (player_id, report_date, source)
)

-- The 167-calls-per-run killer. Content-addressed, so an unchanged
-- article is free forever and a changed one re-summarizes automatically.
-- Grain: (player, sha256 of the article text fed to the model).
news_summaries (
  player_id     text references players,
  article_hash  text,
  summary       text,
  risk_level    text,
  model         text not null,  -- so a model change invalidates cleanly
  created_at    timestamptz not null,
  primary key (player_id, article_hash)
)

-- Fitted model bundles. Grain: (season, week, league, config hash, kind).
model_fits (
  season int, week int,
  league_id     text,
  config_hash   text,
  kind          text,           -- availability | calibration | blend | week1
  bundle        jsonb not null,
  fitted_at     timestamptz not null,
  primary key (season, week, league_id, config_hash, kind)
)

-- APPEND ONLY. Never updated, never deleted.
-- Grain: (run, league, player) -- season/week come from the run.
projections (
  run_id        uuid references runs,
  league_id     text,
  season int, week int,
  player_id     text references players,
  tier          text not null,  -- consensus | in_house_estimate | week1_model
  points              numeric,  -- the EXPECTED value (see CLAUDE.md v17)
  conditional_points  numeric,
  play_probability    numeric,
  floor numeric, ceiling numeric,
  primary key (run_id, league_id, player_id)
)

-- Grain: (league, season, week, player). Recomputed when scoring changes.
actuals (
  league_id     text,
  season int, week int,
  player_id     text references players,
  actual_points numeric not null,
  scored_at     timestamptz not null,
  primary key (league_id, season, week, player_id)
)
```

Notes on choices that aren't obvious:

- **`player_id` stays the nflreadpy `gsis_id`** already used throughout the
  fixtures and `localStorage`. The 2026-08-11 integration plan's open
  question on ID scheme was resolved in favour of real IDs; this schema
  inherits that and does not reopen it.
- **`points` is the expected value**, `conditional_points` the
  if-he-plays number, per v17. Column comments should say so — this is the
  single easiest thing to misjoin in any downstream query.
- **`model` on `news_summaries`** means swapping the summarization model
  invalidates the cache by construction rather than by remembering to
  flush it.
- **`config_hash` on `model_fits` and `runs`** is what makes the
  per-league double-fit disappear: two leagues sharing a scoring config
  hit the same cached bundle.
- **`actuals` is mutable, `projections` is not.** Actuals legitimately
  change when a scoring bug is fixed (three were, in v16). Predictions
  never legitimately change after the fact. Enforce the latter with a
  revoked `UPDATE`/`DELETE` grant on the pipeline's role, not with
  discipline.

The eval layer then becomes a join:

```sql
select p.tier, p.points, a.actual_points
from projections p
join actuals a using (league_id, season, week, player_id)
join runs r on r.run_id = p.run_id
where r.status = 'ok' and (r.season, r.week) = (a.season, a.week);
```

— replacing `generate_track_record.py`'s "read my own past output files
back in" approach entirely.

## What changes in the code

Deliberately little. The store is a cache and a log, not a rewrite.

- **`news.py`** — `summarize_player_news()` gains a lookup by
  `(player_id, sha256(prompt_articles))` before the API call and a write
  after it. Its "never take down the report pipeline" contract is
  unchanged: a store miss or a store outage degrades to calling the API,
  exactly as today.
- **`rotowire.py`** — `fetch_and_cache_pool_stats()` already has the right
  shape (load cache → fetch missing → write back). Only the load/write
  backend changes; the 24-hour TTL logic stays.
- **`calibration_fit.py`, `availability.py`, `week1.py`** — the fit
  functions are pure and already return serialisable bundles. Wrap each
  call site in `generate_report.py` with a `model_fits` lookup keyed on
  `config_hash`.
- **`generate_report.py`** — opens a `runs` row at the top, writes
  `projections` rows alongside (not instead of) the fixture files, closes
  the run at the end. Fixture writing is untouched.
- **`generate_track_record.py`** — the one real rewrite: source predictions
  from `projections` rather than from `weekly-report-week-N.json`.
- **`frontend/`** — no change in Phases 0–2.

Every store interaction should be behind one module (`backend/store.py`)
with a no-op implementation selected when `DATABASE_URL` is unset, so
local runs and the test suite keep working with no database at all. This
is also what makes Phase 0 below shippable on its own.

## Phasing

**Phase 0 — content-addressed caches, no infrastructure (~½ day).**
Introduce `store.py` with a filesystem backend. Key news summaries by
`(player_id, article_hash)` and model fits by `config_hash`. Nothing leaves
the laptop yet, and the cold-runner problem is untouched — but the
*addressing* is fixed, which is the part that makes Phase 1 a backend swap
instead of a rewrite. Ship this even if the rest is never built: it removes
the per-league double-fit and makes a same-day re-run nearly free.

**Phase 1 — Neon, plus the schema above (1–2 days).** Point `store.py` at
Postgres. Pipeline reads the cache tables and writes `runs`/`projections`.
Add `DATABASE_URL` to the workflow's secrets next to the two already
listed in CLAUDE.md's Next Steps item 1. Fixture export unchanged, so the
deployed app cannot regress. This is the phase that fixes both the
cold-runner cost and the mutable prediction log.

**Phase 2 — move the eval layer onto the store (1 day).** Rewrite
`generate_track_record.py` to query `projections ⋈ actuals`. At this point
the track record is genuinely append-only and a regenerated week no longer
edits history.

**Phase 3 — optional, and a real decision: serve `track-record` from a
Vercel serverless read.** The 2 MB / 5 685-row `track-record.json` is the
one fixture whose size is a product problem, and it grows every week of a
real season. A single serverless handler doing a paginated read of
precomputed rows would fix both that and the "every data refresh needs a
git commit and a redeploy" loop. **This is the only phase that touches the
2026-08-16 decision**, and it should be argued separately when there is a
concrete trigger — a real season's worth of history making the download
hurt. It is listed here for completeness, not recommended yet.

## Explicitly out of scope

- **Serving weekly reports or the player pool from the database.** They are
  precomputed JSON on a CDN; a query would be slower and strictly worse.
- **Direct browser access to the store** (Supabase anon key + RLS or
  similar). Buys nothing for a single-user tool and puts a schema and a key
  in the browser.
- **Multi-tenant or auth scaffolding.** CLAUDE.md's "Notes for agents" rules
  this out explicitly and nothing here needs it — `league_id` is a column,
  not a tenant boundary.
- **Migrating `backtest.py` onto the store.** It reads bulk nflreadpy
  history directly and is fine as-is. If scan cost ever bites, the answer
  is a Parquet export, not a different transactional schema.
- **Team config / roster slots.** Stays client-side per
  `docs/specs/team-config-and-roster-status.md` §6.

## Open questions to settle before starting

1. **Neon vs. Supabase vs. Vercel Postgres.** All three are Postgres and all
   three fit free-tier. Neon is the default recommendation here for
   scale-to-zero (this pipeline is idle 167 hours a week) and for not
   dragging in an auth/storage platform that is out of scope. Worth 10
   minutes of confirmation, not a research phase.
2. **Does the store become a hard dependency, or stay optional?** The
   recommendation is optional-by-construction (no `DATABASE_URL` → no-op
   backend → today's behaviour exactly). It costs a little indirection and
   buys a pipeline that still runs on a plane. Confirm before `store.py`'s
   interface is fixed.
3. **What happens to the 13 MB `rotowire_cache_2025.json` already on
   disk?** It is a valid warm cache for 597 players. Recommendation:
   one-shot import into `player_games` on first Phase 1 run rather than
   re-fetching, and then delete the file and its `.gitignore` entry.

## What would make this a mistake

Worth stating plainly, since this project has a documented bias toward not
building infrastructure it doesn't need:

- If the eval layer were not a stated differentiator, the mutable-log
  argument would evaporate and `actions/cache` plus a Phase 0 content-hash
  cache would be the whole correct answer.
- If this stays permanently at one league and one weekly run, the caching
  wins are worth maybe fifteen minutes of CI time a week — real, but not
  on their own worth a database.
- The failure mode to watch for is the store quietly becoming a *second*
  source of truth that disagrees with the fixtures. Phases 0–2 avoid this
  by keeping fixture generation the only thing the frontend ever reads;
  Phase 3 is where that invariant would break, which is a further reason
  to treat it as a separate decision.
