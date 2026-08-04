# NFL Fantasy Value Assistant — Project Brief (v2)

This file is the standing reference for any agent (Claude Code or otherwise) working in this repo. Read it before making architectural or scope decisions.

**2026-08-04 — major update.** Two things changed since v1: (1) the NFL ended its season-long Fantasy Football game for 2026, with ESPN now the official platform — the "NFL Fantasy App" this brief originally referenced no longer exists, and every reference to it below has been replaced or removed; (2) hands-on data-source research (`docs/research/`) replaced the original "evaluate two paid vendors" plan with a decided free-source stack. See "Data Sources" below for what's settled vs. still open.

## Context / Purpose

Portfolio project for an active job search (targeting Senior Product Owner / AI Product Manager roles). The goal is to demonstrate hands-on AI + data-driven product building — not just PM coordination. It will be built with Claude Code, deployed live, documented as a case study, and added to the builder's CV/LinkedIn afterward.

**Builder:** Ágoston Gászner, Senior Product Owner (Applied AI), Budapest. Background in mortgage/lending AI products (LLM document extraction, rule validation, evaluation frameworks) at Intuitech. Plays fantasy football in a 14-team league, migrating from NFL Fantasy to ESPN Fantasy for the 2026 season (see Data Sources — the league's scoring rules and roster need re-confirming post-migration, not assumed carried over as-is).

## Problem

Manually assessing weekly NFL fantasy lineup decisions (start/sit, waiver pickups) requires synthesizing player stats, matchup context, and breaking injury/news — time-consuming to do well every week.

## v1 Scope (2-week build target)

**In scope:**

1. **Player value engine** — projected fantasy points computed from structured stats and forward-looking projections, using the builder's *actual* league scoring rules rather than a vendor's default preset. This matters concretely: the builder's league uses 6-point passing touchdowns (not the 4-point default most sources assume) and unusually granular DST scoring — trusting a vendor's precomputed "points" field directly would misvalue players, especially QBs. The engine needs to compute points from raw stat-line data using the league's real scoring formula.
2. **News & injury layer** — pull player news/injury data; use an LLM to summarize what's relevant and flag risk level (e.g., "questionable, limited practice reps") rather than scraping raw news sources from scratch.
3. **Daily/weekly report output** — start/sit recommendations and top waiver-wire targets, combining the value engine + news layer.
4. **Eval layer (differentiator)** — track the tool's own recommendation accuracy against actual results over the season; a simple accuracy/track record view, ideally public. This is the "AI evaluation framework" instinct from the builder's day job, made into a visible artifact. Ground truth is available for free via `nflreadpy`'s actual game stats — but should also be recomputed against the league's real scoring formula, not taken as a generic PPR/standard total, for the eval numbers to mean anything.

**Explicitly out of scope for v1** (documented as v2 roadmap):

- Trade suggestions (requires full league-state / roster modeling across teams)
- "Fan interaction" sentiment signals (hard to source cleanly, low signal-to-noise)
- **Pulling the builder's live roster/matchups/scoring config directly from ESPN's Fantasy league API** (recommended default — confirm or override). ESPN's league API for private league data is unofficial/undocumented, same as the rest of ESPN's fantasy platform, and requires account-auth (league ID + cookies), not just an API key. Taking on that dependency to save one manual data-entry step isn't worth it for a single-user v1 tool — enter the league's scoring rules and roster once by hand instead. Revisit as a v2 nice-to-have if manual re-entry becomes a real recurring cost.
- Multi-league support, user accounts/auth

## Data Sources — decided, with two items still open (2026-08-04)

v1's original plan was to evaluate two paid vendors (Fantasy Nerds, SportsDataIO) and, if free sources didn't cover projections, fall back to manually copying numbers the NFL Fantasy App displayed for free. Neither premise holds: the paid vendors were priced out (`docs/research/free-data-sources.md`), and the manual-copy fallback no longer exists — there is no more NFL Fantasy App to copy from. That makes the free-source stack below load-bearing, not just a cost optimization.

| Category | Source | Status |
|---|---|---|
| Stats, rosters, snap/usage %, injury designation + practice status | `nflreadpy` | **Confirmed hands-on.** Free, no key, no rate limit. |
| Injury designation cross-check | Sleeper API | **Confirmed hands-on.** Free, no key. Genuinely redundant with nflreadpy — useful for in-season cross-validation, not just a backup. |
| Player-ID crosswalk | DynastyProcess `db_playerids.csv` | **Confirmed hands-on.** Free. Primary for pre-season/rookie coverage; nflreadpy's own roster table covers most IDs for in-season players directly. |
| Consensus rankings (sanity-check baseline only, not a build input) | DynastyProcess `db_fpecr.csv.gz` | **Confirmed hands-on.** Free. Confirmed rank-only — no projected points in this file (corrects an earlier research error that conflated it with FantasyPros' live projections scrape). |
| Weekly fantasy point projections | FantasyPros free-tier API | **Partially confirmed — the plan's single remaining blocker.** Real per-player projected stat lines confirmed, but every response caps at 10 players, and the `scoring` request parameter isn't yet confirmed to actually change the output (in progress). There's no fallback source and no manual-entry fallback anymore if this doesn't pan out — resolving this is the top priority. |
| Matchup difficulty (defense vs. position) | Computed in-house from `nflreadpy` team stats | No source, free or paid, has this as a direct field — confirmed via research, not a gap specific to the free stack. |
| DST/team-defense scoring inputs | `nflreadpy` team stats — **not yet verified** | The league scores defense unusually granularly: forced fumbles and recovered fumbles counted separately, tiered points-allowed and yards-allowed bands, blocked kicks, return yards on the DST slot. Needs a dedicated check that nflreadpy's team-stats table carries fields at this granularity — not yet probed. |
| News (free text, for the LLM summarization layer) | ESPN's unofficial API (`site.api.espn.com/.../news`) | Free, no key, but unofficial and unstable by nature — no ESPN ToS covers this use. The only free-text news source found; needs a degrade-gracefully fallback plan. |
| League scoring rules & roster | Manual entry (v1) | See "out of scope" above. Last known settings are 2025 NFL Fantasy rules (screenshotted); re-verify against the builder's actual 2026 ESPN league once migrated, don't assume they carried over unchanged. |

Full research trail, in order: `docs/research/free-data-sources.md` → `data-source-test-plan.md` → `phase1-probe-results.md` / `phase1-local-results.json` → `coverage-scorecard.md` / `data-points-spec.md` → `operational-reliability-and-crossvalidation.md` → `scope-note-draft.md`.

## Suggested Architecture

- **Frontend:** simple React app (single view: weekly report + eval/track-record view)
- **Backend:** lightweight Node/Python service — fetches from the data sources above, runs LLM summarization on news/injury data, computes value engine output using the league's real scoring formula
- **LLM:** Claude API for news summarization/risk flagging and reasoning
- **Deployment:** Vercel or similar, so it's live and shareable with a link

## Success Criteria for v1

- Tool produces a real, usable weekly report during an actual NFL fantasy week
- Recommendations are tracked against real outcomes (even informally) to seed the eval/track-record layer
- Deployed live with a shareable link
- Documented as a short case study: problem → approach → what was built → what was measured → what's next (v2 roadmap)

## Next Steps (in order)

1. Confirm FantasyPros' free tier honors the `scoring` request parameter — test against a receiving-heavy RB/WR, not a QB (a QB can't distinguish STD from PPR). In progress.
2. Confirm whether FantasyPros supports per-player/ID-targeted queries. This resolves the 10-player-per-response cap; without it, the free projections source may not be usable for a full roster + waiver list, and there's no fallback to fall back to.
3. Migrate the league to ESPN Fantasy; re-confirm the 2026 scoring rules and roster settings against the 2025 rules already captured, rather than assuming they match.
4. Verify `nflreadpy`'s team-stats table covers the DST scoring fields this league actually uses (forced vs. recovered fumbles, blocked kicks, return yards, points-/yards-allowed tiers).
5. Commit the research work in `docs/research/` and `scripts/` to git — currently untracked (verified 2026-08-04); `.env` is correctly gitignored, no key ever entered git history.
6. Build the core value engine: compute fantasy points from each source's raw stat-line data using the league's real scoring formula — never trust a vendor's precomputed STD/PPR/Half-PPR field directly.
7. Add news/injury summarization layer.
8. Build the report output view.
9. Add the eval/track-record view.
10. Deploy, use for a real fantasy week, document as a case study.

## Notes for agents

- This is a portfolio/case-study project — code quality, clear commit history, and a clean architecture matter as much as functionality, since the build itself may be referenced in interviews.
- Prefer simplicity over premature generalization: this is a single-user, single-league tool for v1. Don't build multi-tenant or auth scaffolding unless explicitly asked — this is also the reasoning behind keeping ESPN's private league API out of v1: manual entry of the league's own settings is simpler and avoids an extra unofficial-auth integration for marginal benefit.
- Never trust a vendor's precomputed fantasy-points field without checking it against the builder's actual league scoring rules first. This league's 6-point passing touchdowns and granular DST scoring diverge from standard presets — compute points from raw projected/actual stat lines using the real scoring formula instead.
- Keep the eval/track-record layer in mind from the start — recommendations should be logged in a way that makes later accuracy scoring straightforward (e.g., store the prediction, the context it was made with, and a way to attach the actual outcome once known).
- Data source selection is done — see "Data Sources" above and the linked research docs. Two items remain genuinely open (the FantasyPros cap/scoring-param questions and the DST field-coverage check); don't re-litigate the rest without new evidence.
