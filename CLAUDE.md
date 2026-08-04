# NFL Fantasy Value Assistant — Project Brief (v7)

This file is the standing reference for any agent (Claude Code or otherwise) working in this repo. Read it before making architectural or scope decisions.

**2026-08-04 — v7.** Two corrections to v6:

1. The scoring-rules "schema" track was declared **done** in v6 — that overstated it. `docs/research/espn-scoring-waivers-rules-summary.md` covers the general scoring/waiver categories ESPN's platform supports, but four specific questions are still unconfirmed against a real settings UI: whether forced fumbles are their own scoring category (separate from recovered fumbles), whether a plain non-lost "Fumble" penalty exists as a category, whether offensive yardage-milestone bonuses are configurable, and what happens to defensive/return touchdown points now that ESPN doesn't credit these to the D/ST slot (a likely genuine rule incompatibility with the old league's rules, not just a value to update). See Next Steps item 3 and the Data Sources table.
2. Next Steps is reordered: user-journey/frontend/report-view design is now the current main focus, since it doesn't depend on the real league's scoring values and was previously scheduled behind several data-layer steps that are either still blocked (real values) or premature (value engine, before the journey is designed). The previously discussed full historical backtest harness is downgraded to a small manual sanity check (2–3 real weeks, hand-computed) rather than a build phase — a proper backtest is premature before the frontend and real scoring rules are settled.

(v6's DST field-coverage resolution, v5's DST resolution, v4's two-track scoring-rules split, v3's FantasyPros-cap resolution and two-tier value engine design, and v2's NFL Fantasy App removal / free-source pivot, are carried forward, not repeated here.)

## Context / Purpose

Portfolio project for an active job search (targeting Senior Product Owner / AI Product Manager roles). The goal is to demonstrate hands-on AI + data-driven product building — not just PM coordination. It will be built with Claude Code, deployed live, documented as a case study, and added to the builder's CV/LinkedIn afterward.

**Builder:** Ágoston Gászner, Senior Product Owner (Applied AI), Budapest. Background in mortgage/lending AI products (LLM document extraction, rule validation, evaluation frameworks) at Intuitech. Plays fantasy football in a 14-team league, migrating from NFL Fantasy to ESPN Fantasy for the 2026 season (see Data Sources — the league's scoring rules and roster need re-confirming post-migration, not assumed carried over as-is).

## Problem

Manually assessing weekly NFL fantasy lineup decisions (start/sit, waiver pickups) requires synthesizing player stats, matchup context, and breaking injury/news — time-consuming to do well every week.

## v1 Scope (2-week build target)

**In scope:**

1. **Player value engine** — a two-tier projection, not a single API call:
   - **Top-10-per-position players:** use FantasyPros' free-tier consensus projection (`points`/`points_ppr`/`points_half`, whichever matches the league's format).
   - **Everyone else (bench, committee backfields, most waiver targets):** FantasyPros has no data at all for these players on the free tier (confirmed — see Data Sources). Compute an in-house rate-based projection instead: a rolling-window average of recent `fantasy_points` from `nflreadpy`, adjusted by the opponent's defense-vs-position strength (computed in-house — no source has this as a field). Label this tier visibly as "our estimate" vs. FantasyPros' "consensus projection," rather than presenting both as the same kind of number.
   - **Both tiers:** compute the final point total from raw stat-line data (FantasyPros' underlying projected stats, or nflreadpy's actual stats) using the builder's *real* league scoring rules — never trust a vendor's precomputed points field directly. The league uses 6-point passing touchdowns (not the 4-point default most presets assume) and unusually granular DST scoring; a generic STD/PPR/Half-PPR number would misvalue players, especially QBs.
2. **News & injury layer** — pull player news/injury data; use an LLM to summarize what's relevant and flag risk level (e.g., "questionable, limited practice reps") rather than scraping raw news sources from scratch.
3. **Daily/weekly report output** — start/sit recommendations and top waiver-wire targets, combining the value engine + news layer.
4. **Eval layer (differentiator)** — track the tool's own recommendation accuracy against actual results over the season; a simple accuracy/track record view, ideally public. This is the "AI evaluation framework" instinct from the builder's day job, made into a visible artifact. Ground truth is available for free via `nflreadpy`'s actual game stats — but should also be recomputed against the league's real scoring formula, not taken as a generic PPR/standard total, for the eval numbers to mean anything. Given the value engine now has two projection tiers, the eval layer should track accuracy separately for each — the in-house tier is the one actually worth measuring closely, since FantasyPros' consensus numbers aren't this project's own work.

**Explicitly out of scope for v1** (documented as v2 roadmap):

- Trade suggestions (requires full league-state / roster modeling across teams)
- "Fan interaction" sentiment signals (hard to source cleanly, low signal-to-noise)
- **Pulling the builder's live roster/matchups/scoring config directly from ESPN's Fantasy league API** (recommended default — confirm or override). ESPN's league API for private league data is unofficial/undocumented, same as the rest of ESPN's fantasy platform, and requires account-auth (league ID + cookies), not just an API key. Taking on that dependency to save one manual data-entry step isn't worth it for a single-user v1 tool — enter the league's scoring rules and roster once by hand instead. Revisit as a v2 nice-to-have if manual re-entry becomes a real recurring cost.
- Multi-league support, user accounts/auth

## Data Sources — decided (2026-08-04)

v1's original plan was to evaluate two paid vendors (Fantasy Nerds, SportsDataIO) and, if free sources didn't cover projections, fall back to manually copying numbers the NFL Fantasy App displayed for free. Neither premise holds: the paid vendors were priced out (`docs/research/free-data-sources.md`), and the manual-copy fallback no longer exists — there is no more NFL Fantasy App to copy from. That makes the free-source stack below load-bearing, not just a cost optimization.

| Category | Source | Status |
|---|---|---|
| Stats, rosters, snap/usage %, injury designation + practice status | `nflreadpy` | **Confirmed hands-on.** Free, no key, no rate limit. |
| Injury designation cross-check | Sleeper API | **Confirmed hands-on.** Free, no key. Genuinely redundant with nflreadpy — useful for in-season cross-validation, not just a backup. |
| Player-ID crosswalk | DynastyProcess `db_playerids.csv` | **Confirmed hands-on.** Free. Primary for pre-season/rookie coverage; nflreadpy's own roster table covers most IDs for in-season players directly. |
| Consensus rankings (sanity-check baseline only, not a build input) | DynastyProcess `db_fpecr.csv.gz` | **Confirmed hands-on.** Free. Confirmed rank-only — no projected points in this file. |
| Weekly fantasy projection — top-10-per-position players | FantasyPros free-tier API | **Confirmed hands-on, fully resolved.** Real per-player projected stat lines confirmed. The `scoring` request parameter has no effect (ignored) — doesn't matter, every response already includes `points`/`points_ppr`/`points_half` together, so just read the field matching the league's format. Free tier also rate-limits (hit a `429` during testing) — add a small delay between calls. |
| Weekly fantasy projection — everyone else (bench/waiver tier) | **In-house model** — rolling recent-`fantasy_points` average from `nflreadpy`, adjusted by matchup difficulty | **Decision made 2026-08-04.** Confirmed via 6 different request-parameter attempts (`player_id`, `id`, `fpid`, `limit=50`, `offset`, `page`) that FantasyPros' top-10-per-position cap has no override — it's a hard ceiling, not a pagination limit. No other source (free or paid, per earlier research) has projections for these players either. `nflreadpy` has no player-count ceiling, so this covers everyone FantasyPros can't; not yet built. |
| Matchup difficulty (defense vs. position) | Computed in-house from `nflreadpy` team stats | No source, free or paid, has this as a direct field. Doubles as an input to the in-house projection tier above. |
| DST/team-defense scoring inputs | `nflreadpy` team stats + schedules | **Confirmed hands-on, fully resolved (2026-08-04).** No data gap. Forced fumbles (`def_fumbles_forced`), recovered-fumble yards (`fumble_recovery_opp`/`_yards_opp`), and return yards (`punt_return_yards`/`kickoff_return_yards`) are direct per-team fields. Blocked-kick credit and yards-allowed need a self-join of `team_stats` on `game_id` (reading the opponent's row — confirmed blocks are recorded on the blocked team, not the blocking team, against 64 real 2024–2025 rows); points-allowed needs a join with `load_schedules()`'s `home_score`/`away_score`. Detail: `docs/research/dst-scoring-fields.md`. |
| News (free text, for the LLM summarization layer) | ESPN's unofficial API (`site.api.espn.com/.../news`) | Free, no key, but unofficial and unstable by nature — no ESPN ToS covers this use. The only free-text news source found; needs a degrade-gracefully fallback plan. |
| League scoring rules & roster | Manual entry into a configurable schema (v1) | See "out of scope" above. Two separate open questions (see Next Steps items 2–3): the ***real values*** for the builder's actual league — need the commissioner or a self-view of the migrated league's settings page; last known settings are 2025 NFL Fantasy rules (screenshotted), don't assume they carried over unchanged. The ***schema*** (what categories/values ESPN's platform supports) is **mostly mapped, not fully closed** — `docs/research/espn-scoring-waivers-rules-summary.md` covers offensive scoring categories, D/ST tiers, PPR toggle, and waiver types/timing, but four specific questions remain unconfirmed against a real ESPN settings UI: forced-fumble as its own category, a plain non-lost "Fumble" penalty, offensive yardage-milestone bonuses, and what happens to defensive/return TD points — ESPN hasn't credited these to the D/ST slot since 2019, which is likely a genuine rule incompatibility with the old league's rules, not just a value to update. |

Full research trail, in order: `docs/research/free-data-sources.md` → `data-source-test-plan.md` → `phase1-probe-results.md` / `phase1-local-results.json` → `coverage-scorecard.md` / `data-points-spec.md` → `operational-reliability-and-crossvalidation.md` → `scope-note-draft.md` → `espn-scoring-waivers-rules-summary.md`.

## Suggested Architecture

- **Frontend:** simple React app (single view: weekly report + eval/track-record view)
- **Backend:** lightweight Node/Python service — fetches from the data sources above, runs the two-tier value engine (FantasyPros for top-10/position, in-house rate-based model for everyone else) using the league's real scoring formula, runs LLM summarization on news/injury data
- **LLM:** Claude API for news summarization/risk flagging and reasoning
- **Deployment:** Vercel or similar, so it's live and shareable with a link

## Success Criteria for v1

- Tool produces a real, usable weekly report during an actual NFL fantasy week
- Recommendations are tracked against real outcomes (even informally) to seed the eval/track-record layer
- Deployed live with a shareable link
- Documented as a short case study: problem → approach → what was built → what was measured → what's next (v2 roadmap)

## Next Steps (in order)

1. **User journey / frontend / report-view design (current main focus):** design and start building the report view and overall interaction flow — start/sit recommendations, waiver targets, the eval/track-record view. This doesn't need the real league's scoring values to begin: layout, information architecture, and interaction design are largely independent of the exact point numbers, which only get wired in once the value engine exists. Can run in parallel with items 2–3 below.
2. **Real values (blocked on the commissioner):** get the actual 2026 scoring rules and roster settings for the builder's real league — from the commissioner, or by viewing the real league's ESPN settings page directly once migrated (self-view, not the ESPN API).
3. **Close the remaining schema gaps:** `espn-scoring-waivers-rules-summary.md` covers the general categories ESPN's platform supports, but four specific questions are still open — check these against a real ESPN settings UI (the dummy league already in progress): (a) is forced fumble its own scoring category, separate from recovered fumble; (b) does a plain non-lost "Fumble" penalty exist as a category, or only "Fumble Lost"; (c) are offensive yardage-milestone bonuses (e.g., 300+/400+ passing, 100+/200+ rushing/receiving) configurable; (d) what happens to defensive/return touchdown points, since ESPN hasn't credited these to the D/ST slot since 2019 — this may be a genuine incompatibility with the old league's "DST Touchdowns: 6 points" rule rather than just a value to carry over.
4. Design the in-house projection model for bench/waiver-tier players: rolling-window size (e.g., last 3–4 games), how to handle players with little/no recent data (rookies, players returning from injury), and how the matchup-difficulty adjustment factors in.
5. Build the core value engine: both projection tiers, computing points from raw stat-line data using the league's real scoring formula — never trust a vendor's precomputed STD/PPR/Half-PPR field directly. Includes the DST self-join/schedule-join logic confirmed necessary (see Data Sources — `docs/research/dst-scoring-fields.md`).
6. Add news/injury summarization layer.
7. **Sanity-check, not a backtest harness:** spot-check the value engine's output against 2–3 real historical weeks by hand (known players, known outcomes) once it's built. A full historical backtest simulator was considered and is deliberately downgraded to this — it's more engineering than the timeline and current unknowns (real scoring rules, frontend design) justify right now.
8. Add the eval/track-record view, tracking the in-house tier's accuracy separately from the FantasyPros tier.
9. Deploy, use for a real fantasy week, document as a case study.

## Notes for agents

- This is a portfolio/case-study project — code quality, clear commit history, and a clean architecture matter as much as functionality, since the build itself may be referenced in interviews.
- Prefer simplicity over premature generalization: this is a single-user, single-league tool for v1. Don't build multi-tenant or auth scaffolding unless explicitly asked — this is also the reasoning behind keeping ESPN's private league API out of v1: manual entry of the league's own settings is simpler and avoids an extra unofficial-auth integration for marginal benefit.
- Never trust a vendor's precomputed fantasy-points field without checking it against the builder's actual league scoring rules first. This league's 6-point passing touchdowns and granular DST scoring diverge from standard presets — compute points from raw projected/actual stat lines using the real scoring formula instead.
- The value engine has two projection tiers (FantasyPros consensus for top-10/position, in-house estimate for everyone else) because the free tier's cap has no workaround — this is a permanent architecture feature, not a temporary stopgap to remove later. Keep both tiers clearly labeled to the user as different kinds of numbers.
- Keep the eval/track-record layer in mind from the start — recommendations should be logged in a way that makes later accuracy scoring straightforward (e.g., store the prediction, the context it was made with, and a way to attach the actual outcome once known).
- Stats/projections/injury data sourcing is done — see "Data Sources" above and the linked research docs, including `docs/research/dst-scoring-fields.md`; no items open there. The league scoring-rules *schema* still has four open questions (see Data Sources table and Next Steps item 3) — don't treat it as closed until checked against a real ESPN settings UI. Frontend/journey work (Next Steps item 1) does not need to wait on either open item.
