# 50+ Yard TD Distance Bonus — Data Investigation (2026-08-31)

CLAUDE.md Next Steps item 2 (v14): determine whether nflreadpy's weekly-aggregated
`load_player_stats()` carries data that could power the confirmed-real 50+ yard TD
distance bonus (PTD50/RTD50/RETD50, all +1 on top of base 6 in this league's settings).

## What the bonus is

A flat +1 bonus on top of the base 6-point TD score for any pass / rush / rec TD that
covered 50 or more yards on that play. This is a per-play stat ("was this specific TD
play 50+ yards") rather than a cumulative-yardage milestone (like "did this player gain
400+ total passing yards this game"). The existing `milestones` mechanism in `scoring.py`
handles the latter correctly; it cannot express the former.

## Probe: does load_player_stats() carry a 50+ yard TD column?

Pulled the full column list from `nflreadpy.load_player_stats(seasons=[2025])` against
the real 2025 regular season.

**Answer: No.** There is no column matching `*_tds_50*`, `*_tds_50_plus`, or any similar
pattern. The closest columns found:

| Column | What it tracks | Useful for the bonus? |
|---|---|---|
| `passing_40` | Count of passing plays gaining 40+ yards (all plays, not just TDs) | No — no TD filter, wrong distance threshold |
| `receiving_40` | Count of receiving plays gaining 40+ yards | No — same issues |
| `rushing_40` | Count of rushing plays gaining 40+ yards | No — same issues |
| `fg_long` | Longest field goal distance, not a count | No — different stat category |

None of these give a per-player-week count of 50+ yard TD plays specifically.

The weekly-aggregate `load_player_stats()` table — the same one used for QB/RB/WR/TE/K
stat lines throughout this backend — does not preserve the distance of individual TD plays.
A 3-TD game is just `passing_tds = 3` with no breakdown by whether each was 2 yards or
80 yards.

## Could play-by-play data solve this?

`nflreadpy` exposes `load_pbp()` (confirmed via `dir(nflreadpy)` — it's a real function,
not hypothetical). Play-by-play data would have individual play records with both
`touchdown == 1` and `yards_gained`, making a 50+ yard TD count computable per player
per week via a groupby aggregation.

However, this is a materially different kind of data pull from anything else in this
backend:

- **Size**: PBP is typically 40,000–50,000 rows per season vs. ~5,000–6,000 for weekly
  player stats. Loading it every week for a single bonus category is a ~10x data-volume
  increase on the data-ingestion path.
- **New infrastructure**: `generate_report.py`'s current flow calls `load_player_stats()`
  once and maps rows via `nflreadpy_row_to_stat_line`. PBP would need a separate load,
  a per-player-per-week groupby aggregation, and a join back to the stat-line assembly
  step — a meaningful additional code path.
- **Scoring schema change**: `scoring.py`'s three mechanisms (`linear`, `milestones`,
  `tiers`) are all cumulative-stat mechanisms. A per-play-type bonus doesn't fit any of
  them cleanly. Adding it would either require a fourth mechanism (a `per_play_bonus`
  type keyed on a derived count column) or a one-off special case in `scoring.py` — the
  former is the more honest design but also more scope than this single category warrants
  right now.
- **Impact**: the bonus is +1 per qualifying TD. In a typical game, a skill-position
  player scores 0–2 TDs, and 50+ yard TDs are rare (~18 per team-season in DST data for
  similar per-play events). The scoring error from omitting this is small relative to
  projection uncertainty at the weekly level.

## Decision: defer to v2

The data exists in play-by-play but requires more infrastructure than the benefit justifies
for v1. This is the same category of decision as `matchup.py` (built, tested, rejected
after backtest found the benefit didn't exceed the cost) and `usage.py` (same outcome) —
built or investigated, not shipped when the data says it won't materially improve the
product at v1 scale.

**What this means in practice:** `scoring.py` and `scoring_config.placeholder.json` do
not model this bonus. Every player's projected and actual fantasy points will be off by 0
on most weeks, and off by +1 for each 50+ yard TD in exceptional weeks. This is a known,
documented approximation, not a silent bug.

## v2 path (if it ever matters)

1. Add `load_pbp()` call to the data-ingestion layer, gated behind a flag since it's
   expensive.
2. Aggregate to per-player-per-week "long TD count" columns: `passing_td_50_plus`,
   `rushing_td_50_plus`, `receiving_td_50_plus`.
3. Add a `per_play_bonus` scoring mechanism to `scoring.py` (or reuse `linear` if the
   derived columns are treated as a stat-line field, which they would be after step 2).
4. Add config values to `scoring_config.placeholder.json`'s `linear` block.

The threshold to revisit: if multiple weeks of real results show DST projection accuracy
or start/sit hit rate materially below expectations and a diagnostic points to this
category as the cause. For a rare +1 bonus, that bar is high.
