"""
Kicker (K) stat-line assembly -- closes CLAUDE.md Next Steps item 3's K
half ("Wire in DST/K"). Per docs/research/kicker-scoring-fields.md (the
probe this module implements, done 2026-08-12): kickers show up as a
normal `position == "K"` row in nflreadpy's load_player_stats(), the same
per-player, per-week table QB/RB/WR/TE game logs already come from -- no
self-join needed, unlike DST (see dst.py). This module is the kicker
equivalent of scoring.py's NFLREADPY_OFFENSE_COLUMN_MAP /
nflreadpy_row_to_stat_line: a column map + a pure row mapper, kept in its
own module rather than growing scoring.py's offense-only map, same
separation fantasypros.py's own column map already established.

Two modeling decisions made here, not just a column rename -- both
documented in detail in docs/research/kicker-scoring-fields.md, flagged
again here so they're visible at the point they take effect:

1. nflreadpy tracks FG makes in six distance buckets (0-19/20-29/30-39/
   40-49/50-59/60+), finer than this league's config's three bands
   (fg_made_0_39/fg_made_40_49/fg_made_50_plus). The six nest exactly
   inside the three -- re-bucketing is lossless, not an approximation.
2. nflreadpy tracks blocked FGs/PATs (fg_blocked/pat_blocked) as their own
   outcome, distinct from a normal miss -- but scoring_config.placeholder.json
   has no kicker-side "blocked" category (only DST's def_blocked_kick, on
   the *credit* side). Folded into fg_missed/pat_missed here rather than
   silently dropped, since scoring_config.placeholder.json's actual
   category set is out of scope for this task (CLAUDE.md Next Steps item
   2/the commissioner's call, not a stat-line-assembly decision) -- easy
   to un-fold into a real fg_blocked/pat_blocked category later if the
   real league scoring rules define one.

Split, like every other module here, into pure assembly (this whole
module -- exercised by tests/test_kicker.py with synthetic
load_player_stats()-shaped rows, no network) -- unlike scoring.py/dst.py,
there's no separate network adapter needed here: generate_report.py's
existing load_all_game_logs_nflreadpy already pulls the full
load_player_stats() table (every position, not just POOL_POSITIONS) each
report run, so this module only needs to be wired into that loop's
row-to-stat_line step, not given its own load_* function.
"""

from __future__ import annotations

from typing import Any

# Real distance buckets (docs/research/kicker-scoring-fields.md) -> this
# league's three config bands. Order matters for nothing here (summed),
# but kept ascending for readability against the config's own band order.
FG_MADE_0_39_COLUMNS = ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39")
FG_MADE_50_PLUS_COLUMNS = ("fg_made_50_59", "fg_made_60_")

# Direct 1:1 fields, no re-bucketing needed.
NFLREADPY_KICKER_DIRECT_COLUMN_MAP = {
    "fg_made_40_49": "fg_made_40_49",
    "pat_made": "pat_made",
}

# Blocked kicks are folded into the corresponding miss category -- see
# module docstring's modeling decision #2.
FG_MISSED_COLUMNS = ("fg_missed", "fg_blocked")
PAT_MISSED_COLUMNS = ("pat_missed", "pat_blocked")


def _sum_columns(row: dict[str, Any], columns: tuple[str, ...]) -> float:
    return sum(row.get(c) or 0 for c in columns)


def nflreadpy_kicker_row_to_stat_line(row: dict[str, Any]) -> dict[str, float]:
    """Map one nflreadpy load_player_stats() row (as a dict, position=="K")
    to this project's stat_line shape -- same "sum, then only include a
    category if it's non-zero" pattern as
    scoring.nflreadpy_row_to_stat_line, so a QB/RB/WR/TE row run through
    this function harmlessly returns {} (no kicker columns present)
    rather than raising, and this can be merged with
    scoring.nflreadpy_row_to_stat_line's output for the same row with no
    key collisions (the two modules' category names are entirely
    disjoint)."""
    out: dict[str, float] = {}
    made_0_39 = _sum_columns(row, FG_MADE_0_39_COLUMNS)
    if made_0_39:
        out["fg_made_0_39"] = made_0_39
    made_50_plus = _sum_columns(row, FG_MADE_50_PLUS_COLUMNS)
    if made_50_plus:
        out["fg_made_50_plus"] = made_50_plus
    for nfl_col, our_col in NFLREADPY_KICKER_DIRECT_COLUMN_MAP.items():
        val = row.get(nfl_col)
        if val:
            out[our_col] = val
    fg_missed = _sum_columns(row, FG_MISSED_COLUMNS)
    if fg_missed:
        out["fg_missed"] = fg_missed
    pat_missed = _sum_columns(row, PAT_MISSED_COLUMNS)
    if pat_missed:
        out["pat_missed"] = pat_missed
    return out
