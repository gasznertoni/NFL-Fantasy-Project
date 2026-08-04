# Phase 6 — Scope Note Draft (primary + fallback per data category)

This is the "one-page technical scope note" CLAUDE.md's Next Steps item 2 calls for, now naming a primary + fallback per data category instead of a single vendor, since the plan intentionally combines sources. **Status: final — every row has hands-on evidence, including the one design constraint (FantasyPros' top-10-per-position ceiling) that turned out to have no workaround.**

## Primary + fallback per category

| Category | Primary | Fallback | Confidence |
|---|---|---|---|
| Player identity & rostering | **nflreadpy — confirmed today** (3,137-row rosters pull, real `status` breakdown) | Sleeper (12,207-player dump, also confirmed) | **High — both hands-on confirmed** |
| Cross-source player ID crosswalk | **DynastyProcess `db_playerids.csv`** — best for pre-season/rookie coverage | nflreadpy's own rosters table, which carries most IDs directly for in-season players | **High — hands-on confirmed**, with the documented rookie/`fantasypros_id` gap |
| Recent performance / usage stats | **nflreadpy — confirmed today** (19,421 rows, ~140 columns, incl. `target_share`/`wopr`/`fantasy_points`) | — (no real alternative among these 5, and none needed) | **High — hands-on confirmed**, this was the biggest untested dependency and it came back clean |
| Matchup difficulty (defense vs. position) | Computed in-house from nflreadpy team stats | — | High confidence this must be in-house; low confidence on the computation itself (not designed yet) |
| Weekly fantasy projection — top-10-per-position players | **FantasyPros API — confirmed today**: real `points`/`points_ppr`/`points_half` plus underlying stat-line projections, per player | — | **High — hands-on confirmed**, ignore the `scoring` parameter (confirmed to have no effect; all three formats come back in every response anyway) |
| Weekly fantasy projection — everyone else (bench/waiver-tier players) | **No source in this candidate list has this.** Confirmed via 6 different attempts (`player_id`, `id`, `fpid`, `limit=50`, `offset`, `page`) that FantasyPros' free tier has no override for its top-10-per-position cap — every variant returned the identical 10 names. DynastyProcess has no points data to fall back to either (rank-only, confirmed earlier). | **Recommended v1 mitigation** (your call, not yet built): derive a simple in-house rate-based projection for these players from nflreadpy's real recent-performance data (`target_share`, `carries`, `fantasy_points` trend over last N games) — nflreadpy has no player-count ceiling, so this covers anyone FantasyPros can't. Treat it as a clearly-labeled "our estimate" tier vs. FantasyPros' "consensus projection" tier for top players, rather than pretending both are the same kind of number | **High confidence this is a real gap, not yet designed** — this is a genuine v1 architecture decision, more consequential than anything else this scope note surfaced |
| Consensus rankings (ECR) | **DynastyProcess — confirmed today** (1,528,918 rows, `ecr/sd/best/worst/rank_delta/ecr_type`, filter by `scrape_date`) | FantasyPros API | **High — hands-on confirmed** |
| Injury designation | **nflreadpy + Sleeper — both confirmed today**, genuinely redundant (`report_status` vs. `injury_status`/`injury_notes`) | Whichever proves faster/more granular in-season becomes primary; see cross-validation notes | **High — hands-on confirmed on both sides** |
| Practice participation detail | **nflreadpy + Sleeper — both confirmed today** (`practice_status` vs. `practice_participation`/`practice_description`) | Same as above — genuine redundancy, not a single point of failure | **High — hands-on confirmed**, resolves what was "genuinely unknown per the original plan" |
| News/narrative injury text | ESPN unofficial endpoint (per `free-data-sources.md`, not part of this 5-source list but noted as the only free-text option) | — | Low — unofficial, no SLA, needs its own reliability watch |
| Eval-layer ground truth (actual fantasy points scored) | **nflreadpy — bonus finding** (`fantasy_points`/`fantasy_points_ppr` columns, already computed per game) | — | **High — hands-on confirmed**, not called for by name in the original plan but directly useful for the accuracy-tracking layer CLAUDE.md wants |

## Quota math

Only Sleeper, FantasyPros, and API-Sports are call-metered; nflreadpy and DynastyProcess are bulk file pulls with no real quota constraint.

**Worked example (update once you know your real numbers):**

Assume a single-user, single-league weekly report covering a 16-player active roster plus ~15 waiver-wire candidates you check each week = **~31 players**, and 2 data categories pulled per player that require a metered call (e.g., stats + injury, if not available via a bulk endpoint) = **~62 calls per weekly report**, run roughly once a week during the season (plus ad hoc re-checks, say 2-3x/week during injury-news-heavy periods) = **~150-200 calls/week, ~25-30/day average, spikier around game days.**

- **Sleeper**: no published hard daily cap (soft rate-limiting only) — not a constraint at this scale.
- **API-Sports free tier**: 100 req/day (per `free-data-sources.md`, still unverified against your own dashboard). At ~25-30/day average this fits, but a same-day multi-check during injury news (Wed/Thu practice reports + Sunday inactives) could spike past 100 if pulled per-player rather than via a bulk team/roster endpoint. **Recommendation: use API-Sports only as a narrow fallback/cross-check**, not a primary pull path — the free quota isn't built for a full weekly pipeline.
- **FantasyPros free tier**: **confirmed today, and confirmed unfixable via any request parameter — the binding constraint is a hard 10-player-per-position cap**, not a daily request cap. `player_id`, `id`, `fpid`, `limit=50`, `offset`, and `page` were all tried against a real player (Travis Kelce, `fpid=11594`) and none changed the response. Your ~31-player weekly coverage need (16 roster + 15 waiver) will only ever get FantasyPros numbers for whichever of those players happen to rank in their position's top 10 — there is no query-your-way-around-it option on this tier. A paid tier is the only way to lift this ceiling from FantasyPros itself.

**Recompute the request-count math once you have a real API-Sports dashboard number.**

## Status: final — one architecture decision left, not a data question

Updated 2026-08-04 after fully probing the FantasyPros free tier. Every data-*source* question from the original 5-source plan is now answered with real hands-on evidence, including a definitive negative on FantasyPros' cap. What's left is a build decision, not more research:

1. **How to handle projections for players outside FantasyPros' top-10-per-position.** Recommended default (see the projection row above): a simple in-house estimate from nflreadpy's recent-performance data for everyone FantasyPros can't cover, clearly labeled as a different kind of number than the consensus projection. Alternative: accept the gap for v1 and only show projections for top-10-per-position players; alternative: pay for a higher FantasyPros tier. This is a real product decision worth making deliberately rather than defaulting into it.
2. **API-Sports key** — optional, narrow fallback role only, not blocking anything above.

This note is ready to hand off as the real technical scope note for CLAUDE.md's Next Steps item 2.
