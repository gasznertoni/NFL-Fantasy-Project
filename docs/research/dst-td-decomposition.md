# DST TD Column Decomposition — Resolved (2026-08-31)

CLAUDE.md Next Steps item 1 (v14): narrow `backend/dst.py`'s `def_st_td` mapping to
match the real ESPN league rule. The real settings show only one explicit TD-credit line:
"Fumble Recovered for TD (FTD) = 6." No separate INT-return-TD or
punt/kickoff-return-TD bonus line appears anywhere in the Passing / Rushing / Receiving /
Kicking / Miscellaneous / Defensive-Players sections. `dst.py`'s previous mapping summed
`def_tds` + `special_teams_tds`, crediting all three TD types the same way — broader than
the real rule. This doc records the probe that found the correct column and confirmed the
fix.

## What the old mapping covered

| Old nflreadpy column | What it actually tracks | In real ESPN settings? |
|---|---|---|
| `def_tds` | INT-return TDs + fumble-return TDs combined | No separate line — only fumble-return TD gets 6 pts |
| `special_teams_tds` | Punt- and kickoff-return TDs | No separate line anywhere in the settings |

`def_tds` had 28 nonzero game rows in 2025. `special_teams_tds` had 26. Both were being
credited at +6 each under the old mapping — every INT-return TD and return TD was getting
the same +6 that the real rule intends only for fumble-recovery TDs.

## Probe: can def_tds be decomposed at weekly-aggregate granularity?

Checked `load_team_stats()` column list against a real 2025 season pull.

**Answer: No — `def_tds` has no per-return-type breakdown in weekly aggregate data.** The
column is a single combined count with no fumble-vs-INT split at this granularity. Play-
by-play data (`load_pbp()`) would theoretically allow the split, but that's a much larger
data pull and `fumble_recovery_tds` makes it unnecessary anyway (see below).

## Discovery: fumble_recovery_tds is a direct column

`load_team_stats()` carries `fumble_recovery_tds` as its own separate column, distinct
from `def_tds`. Probe results against the full 2025 regular season (all 544 team-game rows
after filtering to `season_type == "REG"`):

| Column | Nonzero game rows | Sample values |
|---|---|---|
| `fumble_recovery_tds` | **18** | [1, 1, 1] |
| `def_tds` | 28 | [1, 1, 1] |
| `special_teams_tds` | 26 | [1, 1, 1] |

`fumble_recovery_tds` is precisely "fumble recoveries that resulted in a TD scored by this
team" — the one category ESPN credits to the DST in this league's settings. The difference
between `def_tds` (28) and `fumble_recovery_tds` (18) represents INT-return TDs that the
old mapping was incorrectly crediting.

Also confirmed present on the same table: `fumble_recovery_opp` (opponent fumbles
recovered by this team, already mapped to `def_fumble_rec`) and `fumble_recovery_yards_opp`
— all on the DST's own row, no join needed.

## Fix applied

`DST_DIRECT_COLUMN_MAP` in `backend/dst.py` now contains:

```python
"fumble_recovery_tds": "def_st_td",
```

`DST_TD_COLUMNS = ("def_tds", "special_teams_tds")` and the three-line block that
computed their sum are removed. `def_tds` and `special_teams_tds` are no longer read at
all. `assemble_dst_stat_line` now produces `def_st_td` from `fumble_recovery_tds` via the
same `DST_DIRECT_COLUMN_MAP` loop that handles all other direct fields — reads from
`own_row`, skips on 0/None, maps to `def_st_td`.

## Tests updated

`tests/test_dst.py`'s `test_st_td_sums_def_tds_and_special_teams_tds` replaced with
`test_st_td_uses_fumble_recovery_tds_only`, which:
1. Confirms `fumble_recovery_tds = 1` → `stat_line["def_st_td"] == 1` even when
   `def_tds` and `special_teams_tds` are also present
2. Confirms `def_tds` and `special_teams_tds` alone (no `fumble_recovery_tds`) produce
   no `def_st_td` entry at all

All 15 `test_dst.py` cases pass.

## Confidence

High. `fumble_recovery_tds` is a named, independently-tracked column (not inferred or
computed), confirmed real and nonzero against a full season's data. Its semantics match
the ESPN rule exactly. The previous mapping was provably over-counting TD credits by
including INT-return and return-TD types that have no bonus line in this league's settings.
