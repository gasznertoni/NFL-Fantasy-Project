# DST/Team-Defense Scoring Field Coverage — Resolved (2026-08-04)

CLAUDE.md's next-step item: verify `nflreadpy`'s team-stats table carries the fields this
league's granular DST scoring needs (forced vs. recovered fumbles, blocked kicks, return
yards, tiered points-/yards-allowed bands). Probed hands-on via `scripts/probe_sources.py`
(`probe_dst_fields`, results in `phase1-local-results.json` → `sources.dst_fields`).

## Result: no data gap — three of five needs are direct fields, two need an in-house join

| League DST need | Coverage | Detail |
|---|---|---|
| Forced fumbles | **Direct field** | `def_fumbles_forced` |
| Recovered fumbles + return yards | **Direct field** | `fumble_recovery_opp` / `fumble_recovery_yards_opp` (recovering the opponent's fumble — the DST-relevant case, distinct from `fumble_recovery_own`) |
| Return yards on the DST slot | **Direct field** | `punt_return_yards`, `kickoff_return_yards` (plus `punt_returns`/`kickoff_returns` counts) |
| Blocked kicks | **Needs a self-join** | `fg_blocked`/`pt_blocked` exist, but are recorded on the row of the team whose kick got blocked, not the blocking team — confirmed against 64 real blocked-kick rows in 2024–2025 data. Crediting the *defense* means reading `opponent_team`'s `fg_blocked`/`pt_blocked` for the same `game_id`. |
| Tiered yards-allowed bands | **Needs a self-join** | No `yards_allowed` field. Compute from the opponent's own `passing_yards` + `rushing_yards` on their row for the same `game_id`. |
| Tiered points-allowed bands | **Needs a join with `load_schedules()`** | `team_stats` has no points/score field at all. `load_schedules()` has `home_score`/`away_score` per `game_id` — join on that. |

## Conclusion

Everything the league's DST scoring formula needs is present in `nflreadpy`. Three fields
apply directly per-team with no extra work; blocked-kick credit and yards-allowed both
need a self-join of `team_stats` against itself on `game_id` (reading the opponent's row);
points-allowed needs a join against `load_schedules()`. This is in-house engineering work
in the value engine, not a data-source gap — no fallback source needed, nothing blocked.

This closes the last open item from `coverage-scorecard.md`. See CLAUDE.md's Data Sources
table for the resolved status.
