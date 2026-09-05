-- Shared data store, phase 1.
-- Schema from docs/design/shared-data-store.md. Run once against the target
-- Postgres (Neon by recommendation, but nothing here is Neon-specific):
--
--     psql "$DATABASE_URL" -f backend/migrations/001_shared_data_store.sql
--
-- Grain is stated on every table, because the silent-column-map lesson from
-- v16 applies to joins too: a join at the wrong grain produces a plausible
-- wrong number rather than an error.

begin;

-- Provenance. Grain: one row per generate_report.py invocation.
create table if not exists runs (
  run_id        uuid primary key,
  season        int  not null,
  week          int  not null,
  git_sha       text,
  config_hash   text,
  started_at    timestamptz not null,
  finished_at   timestamptz,
  status        text not null default 'running'
                check (status in ('running', 'ok', 'failed'))
);
create index if not exists runs_season_week_idx on runs (season, week);

-- Slowly-changing player identity. Grain: one row per player.
-- player_id is the nflreadpy gsis_id already used throughout the fixtures and
-- localStorage; the 2026-08-11 integration plan settled the ID scheme in
-- favour of real IDs and this schema inherits that rather than reopening it.
create table if not exists players (
  player_id     text primary key,
  name          text not null,
  position      text not null,
  team          text,
  espn_id       text,
  updated_at    timestamptz not null
);

-- Shared upstream cache. Grain: (player, season, week, source).
create table if not exists player_games (
  player_id     text not null references players (player_id),
  season        int  not null,
  week          int  not null,
  source        text not null,          -- nflreadpy | rotowire
  stat_line     jsonb not null,
  fetched_at    timestamptz not null,
  primary key (player_id, season, week, source)
);
create index if not exists player_games_season_source_idx on player_games (season, source);

-- Grain: (player, report date, source).
create table if not exists injury_reports (
  player_id       text not null references players (player_id),
  report_date     date not null,
  source          text not null,        -- espn | sleeper | nflreadpy
  designation     text,
  practice_status text,
  raw             jsonb,
  primary key (player_id, report_date, source)
);

-- The 167-calls-per-run killer. Content-addressed, so an unchanged article is
-- free forever and a changed one re-summarises automatically. `model` means a
-- model swap invalidates by construction rather than by remembering to flush.
-- Grain: (player, sha256 of the article text fed to the model).
create table if not exists news_summaries (
  player_id     text not null,
  article_hash  text not null,
  summary       text,
  designation   text,                  -- see note below
  risk_level    text,
  model         text not null,
  created_at    timestamptz not null,
  primary key (player_id, article_hash)
);

-- `designation` is a DELIBERATE ADDITION to the design doc's schema, which
-- listed only summary/risk_level. news.apply_sleeper_designation overrides
-- designation and riskLevel with Sleeper's value and calls it authoritative,
-- which makes storing it look redundant -- but it returns the flag UNCHANGED
-- when Sleeper has no entry for that player, and then the model's own
-- designation is what survives. Caching without this column would replace it
-- with 'Healthy' on every hit: a cache that quietly downgrades an injury flag,
-- which is worse than no cache at all.

-- Fitted model bundles. Grain: (season, week, league, config hash, kind).
-- Lookups key on config_hash and IGNORE league_id, which is what makes the
-- per-league double-fit disappear: two leagues sharing a scoring config hit
-- the same bundle. league_id is kept for provenance only.
create table if not exists model_fits (
  season        int  not null,
  week          int  not null,
  league_id     text not null,
  config_hash   text not null,
  kind          text not null
                check (kind in ('availability', 'calibration', 'blend', 'week1')),
  bundle        jsonb not null,
  fitted_at     timestamptz not null,
  primary key (season, week, league_id, config_hash, kind)
);
create index if not exists model_fits_lookup_idx
  on model_fits (season, week, config_hash, kind);

-- APPEND ONLY. Never updated, never deleted. This table is the reason the
-- whole design exists: it replaces "read my own past output files back in",
-- where regenerating week 5 in November silently rewrote October's prediction
-- and then graded today's model against October's outcomes.
-- Grain: (run, league, player) -- season/week come from the run.
create table if not exists projections (
  run_id              uuid not null references runs (run_id),
  league_id           text not null,
  season              int  not null,
  week                int  not null,
  player_id           text not null,
  tier                text not null,   -- consensus | in_house_estimate | week1_model
  points              numeric,         -- EXPECTED value: P(play) x conditional
  conditional_points  numeric,         -- the if-he-plays number
  play_probability    numeric,
  floor               numeric,
  ceiling             numeric,
  primary key (run_id, league_id, player_id)
);
create index if not exists projections_eval_idx
  on projections (league_id, season, week, player_id);

comment on column projections.points is
  'EXPECTED value = play_probability x conditional_points (CLAUDE.md v17). '
  'Do NOT compare against a vendor projection or a raw rolling average, which '
  'are conditional-on-playing numbers. This is the single easiest column to '
  'misjoin in any downstream query.';
comment on column projections.conditional_points is
  'Points if the player suits up. The number Sleeper and most fantasy apps '
  'display.';

-- Grain: (league, season, week, player). Mutable ON PURPOSE -- actuals
-- legitimately change when a scoring bug is fixed, and three were in v16.
-- Predictions never legitimately change after the fact, which is why
-- `projections` above is not.
create table if not exists actuals (
  league_id     text not null,
  season        int  not null,
  week          int  not null,
  player_id     text not null,
  actual_points numeric not null,
  scored_at     timestamptz not null,
  primary key (league_id, season, week, player_id)
);

commit;

-- ---------------------------------------------------------------------------
-- Append-only enforcement.
--
-- The design doc is explicit that this belongs in a grant, not in discipline:
-- "Enforce the latter with a revoked UPDATE/DELETE grant on the pipeline's
-- role, not with discipline." Run these AFTER creating the role the pipeline
-- connects as, substituting its name. Left commented because the role name is
-- deployment-specific and a wrong name here would fail the whole migration.
--
--   revoke update, delete on projections from fantasy_pipeline;
--   grant  insert, select on projections to   fantasy_pipeline;
--
-- Everything else the pipeline touches is a cache or a mutable fact, so the
-- usual grants apply:
--
--   grant select, insert, update on
--     runs, players, player_games, injury_reports,
--     news_summaries, model_fits, actuals
--   to fantasy_pipeline;
-- ---------------------------------------------------------------------------
