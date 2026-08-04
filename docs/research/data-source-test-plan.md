# Data Source Decision & Testing Plan (Value/Projection Engine)

Date: 2026-08-04. Scope: this plan covers data needs for the **value/projection engine only** (the model that generates start/sit and waiver recommendations) — not the separate accuracy-tracking eval layer, whose data needs (real box scores after the fact) are narrower and already well covered by `nflreadpy` alone; scope that one separately when you get to it.

Candidate sources (per the risk bar you set — official/documented low-risk plus DynastyProcess, excluding ESPN's unofficial API):

1. `nflreadpy` — stats, schedules, rosters, snap counts, structured injury status
2. Sleeper API — player IDs, status flags, trending
3. DynastyProcess/`ffpros` (FantasyPros redistribution) — rankings + projections + player-ID crosswalk
4. FantasyPros' own free-tier API — rankings/projections, official path
5. API-Sports (American Football) — general stats/injuries, official, 100 req/day free

## Why I'm not doing "spec first, API question fully open"

Two known gaps already surfaced in the prior research would blow up a spec written without any hands-on API contact:

- **No source — free or paid — has a "matchup difficulty" endpoint.** If the spec assumes one exists as an input, it'll need computing in-house from raw team defense stats, which changes both the spec and the engineering effort. Better to know that before finalizing scope than after.
- **Player IDs don't match across sources.** A spec that assumes you can freely join nflreadpy + Sleeper + FantasyPros data by player identity will silently break without DynastyProcess's `db_playerids.csv` crosswalk (or an equivalent you build yourself).

So the plan below still separates "what do I need" from "which API provides it," as you proposed — but it interleaves a short, cheap hands-on probe *before* the data-points list is finalized, not after. The probe is a few hours of scripting, not a full build; it exists purely to stop the spec from being written against assumptions.

## Phase 0 — First-principles data requirements

Write this from what a good start/sit + waiver decision actually needs, independent of any vendor's marketing page. Suggested categories to work through (add/cut based on your own judgment of what a fantasy manager actually weighs):

- Player identity & rostering: position, team, active/inactive, bye week
- Recent performance: last N games' stats, season-to-date stats, usage trend (snap %, target share)
- Matchup context: opponent, opponent's defensive strength vs. that position — **known gap, likely computed in-house from raw team stats, not pulled as a single field**
- Forward-looking projection: weekly fantasy points by scoring format (standard/PPR/half-PPR)
- Consensus rankings (ECR): useful as a sanity-check baseline against your own model's output, not necessarily a direct input
- Injury/practice status: designation (Questionable/Doubtful/Out) and ideally practice participation trend, not just game-day status
- Game context (home/away, Vegas implied team total, weather): **flag as likely unavailable in any free source in this list** — don't assume it's there; if you want it later it's a separate, harder sourcing problem (odds APIs are a different category entirely)

Mark each item, at write time, as "confirmed available" (only after Phase 1), "computed in-house," or "out of scope for v1" — resist writing the spec with unverified assumptions in the "confirmed available" column.

## Phase 1 — Hands-on shape probe (do this before finalizing Phase 0's list)

For each of the 5 candidate sources, make one real call per relevant data category and actually look at the response — not the docs page. This is the step that was skipped in the earlier research pass (that pass was explicitly marketing-page/search-snippet based, not hands-on).

For each pull, record: exact field names, grain (per-game vs. weekly vs. season-to-date), which players are present/missing (check a few known edge cases: a rookie, a practice-squad player, a recently-traded player), and the timestamp/last-updated marker if the response includes one.

Suggested minimum probe set:

| Source | Pull | What to check |
|---|---|---|
| `nflreadpy` | `load_player_stats()`, `load_injuries()`, `load_rosters()` for 2025 season | Field names, whether injury status has a practice-participation field or only game-day designation |
| Sleeper | `GET /v1/players/nfl` and a trending-players call | Whether status/injury fields exist, how stale the player dump is |
| DynastyProcess | `db_fpecr.csv.gz` and `db_playerids.csv` | Whether projections (not just rankings) are actually in this file or only ECR; confirm the ID crosswalk covers your target players |
| FantasyPros API | Request a free key, hit the rankings/projections endpoint for one week | Whether the *free* tier actually includes projections, or only rankings (this is unverified — don't assume) |
| API-Sports | One players/stats call for a known player | Response shape, whether a single call burns 1 or multiple quota units |

## Phase 2 — Write the grounded data-points spec

Now fill in Phase 0's list using what Phase 1 actually showed, not what was hoped for. Anything that turned out unavailable everywhere gets explicitly marked "computed in-house" or "out of v1 scope" in the spec itself, so it's a documented decision rather than a silent gap discovered mid-build.

## Phase 3 — Coverage scorecard

Build a simple matrix: rows = your finalized data points, columns = the 5 sources, cells = full / partial / none, based on Phase 1 findings. This becomes the primary artifact for picking a primary source per data category (and a backup, since you're combining sources — see Phase 5 on why backups matter here).

## Phase 4 — Operational reliability test

Important timing constraint: **today is preseason** (2026 regular season starts September 9; roster cuts to 53-man happen in late August). That means:

- Live weekly injury-status freshness, in-season projection updates, and real waiver-wire dynamics **can't be tested against live current-season data yet.**
- What you *can* test now: correctness and completeness against the **completed 2025 season** (backtest known weeks against known outcomes — good for validating stats/projection *shape* and the value engine's logic), plus current offseason signals like roster-cut and depth-chart churn in late August, which nflreadpy and Sleeper both track.
- Plan a second, shorter reliability pass once the season starts (e.g., the first 2–3 live weeks) specifically for freshness and uptime, since that's the one thing that can't be verified before kickoff.

For whichever sources you can test now, log over a real multi-day window: error rate, latency, schema drift day-to-day, and whether expected players are missing.

## Phase 5 — Cross-validation between overlapping sources

Where two sources cover the same field (e.g., injury status from both `nflreadpy` and Sleeper), pull both for the same week and diff them. Disagreement is a useful signal — either a source is stale/wrong, or you've found a genuine edge case (e.g., a status update that hit one feed before the other). Decide, per overlapping field, which source is primary and which is a secondary/confirmation check — this is also your practical redundancy plan if one source degrades mid-season.

## Phase 6 — Decision + write the scope note

Once Phases 3–5 are done, this directly produces CLAUDE.md's already-planned "Next Steps" item 2 (a one-page technical scope note with exact endpoints/fields once the source is confirmed) — except now it can name a primary + fallback per data category instead of a single vendor, since you're intentionally combining sources.

## Quota math (only matters for 2 of the 5 sources)

`nflreadpy` and DynastyProcess are bulk file/dataframe pulls, not per-request metered — quota isn't a real constraint for either. Sleeper, the FantasyPros API, and API-Sports are call-based with daily caps, so before committing, compute actual call volume for your use case (single-user, single-league, one weekly report): roughly (roster size) × (categories pulled per player) per report, checked against each source's published free-tier cap — don't assume "100 requests/day" is obviously enough or obviously not enough without doing that multiplication for your actual roster size and report frequency.
