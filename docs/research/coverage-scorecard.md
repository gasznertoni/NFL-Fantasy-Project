# Phase 3 — Coverage Scorecard

Rows = finalized-ish data points from `data-points-spec.md`. Columns = the 5 candidate sources. Cells: `full` / `partial` / `none` / `pending` (probe not yet run) / `blocked` (needs your API key).

| Data point | nflreadpy | Sleeper | DynastyProcess | FantasyPros API | API-Sports |
|---|---|---|---|---|---|
| Position / team / active-inactive | **full — confirmed** | **full — confirmed** | partial (crosswalk only, no status) | blocked | blocked |
| Bye week | full (per docs, not separately probed) | none | — | — | pending |
| Cross-source player ID | **full — confirmed** (rosters carry most IDs directly) | partial (own IDs only) | **full** (confirmed today, rookie gap on `fantasypros_id` noted) | — | — |
| Last-N-games / season-to-date stats | **full — confirmed** (19,421 rows, ~140 columns, incl. `fantasy_points`) | none | none | blocked | pending |
| Snap %/target share (usage trend) | **full — confirmed** (`target_share`, `air_yards_share`, `wopr`, `racr`) | none | none | none | none |
| Opponent | full (`opponent_team` column confirmed present) | none | none | none | pending |
| Defense-vs-position matchup | none (computed in-house from raw stats) | none | none | none | none |
| Weekly fantasy projection | none | none | **none — confirmed today, rank-only file** | **partial — confirmed today**: real projections exist (`points`/`points_ppr`), but capped at 10 players/response on the free tier | blocked |
| Consensus rankings (ECR) | partial (indirectly loads DynastyProcess's file per docs) | none | **full — confirmed today** | blocked | none |
| Injury designation (Q/D/O) | **full — confirmed** (`report_status` + injury detail, 6,068 rows) | **full — confirmed** | none | none | pending |
| Practice participation trend | **full — confirmed** (`practice_status` + detail fields) | **full — confirmed** | none | none | none |
| Narrative injury/news text | none | none | none | none | limited (per earlier research) |

## Reading this table honestly

Updated after the second local script run (2026-08-04, nflreadpy now working). Nearly every cell is now hands-on confirmed rather than assumed:

- **Cross-source player ID**: DynastyProcess is the clear primary for pre-season/rookie coverage; nflreadpy's own rosters table also carries most IDs directly for in-season players.
- **Defense-vs-position matchup**: confirmed nobody has this; in-house computation is not a hedge, it's the only option.
- **Weekly fantasy projection**: resolved, and not in the plan's favor. DynastyProcess's file doesn't have it (rank-only); FantasyPros' free tier does — real `points`/`points_ppr`/`points_half` numbers confirmed — but is hard-capped at the top 10 per position with no override (6 request-parameter variants tried, all identical). No source in this candidate list covers bench/waiver-tier players' projections. See `scope-note-draft.md` for the recommended mitigation.
- **Injury designation + practice participation**: confirmed both nflreadpy *and* Sleeper have full designation and practice-participation detail — genuine redundancy, good for the Phase 5 cross-check once both have same-week in-season data.
- **Stats/usage**: confirmed nflreadpy has everything needed, including pre-computed `fantasy_points`/`fantasy_points_ppr` — a nice bonus for the eval layer.
- Remaining `blocked` cells are gated on your API-Sports key — see `api-key-setup-todo.md`. That's now the only fully unprobed source left.
