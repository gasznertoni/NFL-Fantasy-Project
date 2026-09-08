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

# nflreadpy's six native FG distance buckets, emitted 1:1 under the same
# names. Added 2026-09-02 for league-2, whose real ESPN settings score all six
# separately (3/3/3/4/5/6). league-1 rolls three of them up instead -- see
# FG_MADE_0_39_COLUMNS above -- so BOTH families are emitted and each league's
# config picks up only the keys it actually defines. A config must use one
# family or the other; validate_fg_band_family() enforces that, because a
# config defining both would double-count every made field goal.
NFLREADPY_KICKER_NATIVE_BUCKETS = {
    "fg_made_0_19": "fg_made_0_19",
    "fg_made_20_29": "fg_made_20_29",
    "fg_made_30_39": "fg_made_30_39",
    "fg_made_50_59": "fg_made_50_59",
    "fg_made_60_": "fg_made_60_plus",
}

# Direct 1:1 fields, no re-bucketing needed. fg_made_40_49 is a native bucket
# AND a league-1 band under the same name, so it belongs to both families.
NFLREADPY_KICKER_DIRECT_COLUMN_MAP = {
    "fg_made_40_49": "fg_made_40_49",
    "pat_made": "pat_made",
}

# The two mutually exclusive band families, for config validation.
ROLLED_BANDS = ("fg_made_0_39", "fg_made_50_plus")
NATIVE_BANDS = ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39",
                "fg_made_50_59", "fg_made_60_plus")

# MISSED field goals have the same two-family problem, added 2026-09-06 for
# league-1's final settings: it charges -2 for a missed kick inside 40 and only
# -1 from 40-49, and defines no line at all beyond 50 -- so a missed 55-yarder
# is free. league-2 charges one flat -1 at every distance. Both families are
# emitted; a config defining both would double-charge every miss.
FLAT_MISS_BAND = ("fg_missed",)
BANDED_MISS_BANDS = ("fg_missed_0_39", "fg_missed_40_49",
                     "fg_missed_50_59", "fg_missed_60_plus")

FG_MISSED_0_39_COLUMNS = ("fg_missed_0_19", "fg_missed_20_29", "fg_missed_30_39")


def validate_fg_band_family(linear_config: dict) -> None:
    """Raise if a scoring config mixes the rolled and native FG band families.

    Both families are emitted into every kicker stat line, so a config that
    defines keys from both would score the same made kick twice. This is
    exactly the class of silent, plausible-looking error the v16 audit found
    three of -- so it raises rather than warning."""
    rolled = [k for k in ROLLED_BANDS if k in linear_config]
    native = [k for k in NATIVE_BANDS if k in linear_config]
    if rolled and native:
        raise ValueError(
            "scoring config mixes FG band families and would double-count made "
            f"field goals: rolled {rolled} alongside native {native}. Use one "
            "family only -- either fg_made_0_39/fg_made_40_49/fg_made_50_plus, "
            "or the six native fg_made_0_19...fg_made_60_plus buckets."
        )

# Blocked kicks are folded into the corresponding miss category -- see
# module docstring's modeling decision #2.
FG_MISSED_COLUMNS = ("fg_missed", "fg_blocked")
PAT_MISSED_COLUMNS = ("pat_missed", "pat_blocked")


def validate_fg_miss_band_family(linear_config: dict) -> None:
    """Raise if a scoring config mixes the flat and banded FG-miss families.

    Same failure mode, and same reasoning, as validate_fg_band_family(): both
    families are emitted into every kicker stat line, so a config defining keys
    from both charges each missed kick twice."""
    flat = [k for k in FLAT_MISS_BAND if k in linear_config]
    banded = [k for k in BANDED_MISS_BANDS if k in linear_config]
    if flat and banded:
        raise ValueError(
            "scoring config mixes FG-miss families and would double-charge "
            f"missed field goals: flat {flat} alongside banded {banded}. Use "
            "one family only -- either fg_missed, or the banded "
            "fg_missed_0_39/fg_missed_40_49/fg_missed_50_59/fg_missed_60_plus."
        )


def _sum_columns(row: dict[str, Any], columns: tuple[str, ...]) -> float:
    return sum(row.get(c) or 0 for c in columns)


def _blocked_distances(row: dict[str, Any]) -> list[float]:
    """Distances of this game's blocked FGs, from nflreadpy's
    `fg_blocked_list` -- a ";"-separated string ("36;44" for a two-block
    game). `fg_blocked_distance` is NOT usable here: it is the SUM of the
    distances (80 for that same game), not a distance.

    A block is folded into the miss band matching its distance, keeping the
    module's standing decision that a blocked kick counts as a miss (docstring
    modeling decision #2) while respecting league-1's distance bands. If the
    list is missing or unparseable the blocks are dropped rather than guessed
    into a band -- charging the wrong band is worse than charging nothing, and
    this is a 24-kicks-a-season category."""
    raw = row.get("fg_blocked_list")
    if not raw or not isinstance(raw, str):
        return []
    out = []
    for part in raw.replace(",", ";").split(";"):
        part = part.strip()
        if part:
            try:
                out.append(float(part))
            except ValueError:
                continue
    return out


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
    if row.get("position") == "K":
        out["position"] = "K"
    # Rolled family (league-1).
    made_0_39 = _sum_columns(row, FG_MADE_0_39_COLUMNS)
    if made_0_39:
        out["fg_made_0_39"] = made_0_39
    made_50_plus = _sum_columns(row, FG_MADE_50_PLUS_COLUMNS)
    if made_50_plus:
        out["fg_made_50_plus"] = made_50_plus
    # Native family (league-2). Safe to emit alongside the rolled family
    # because no config may define both -- see validate_fg_band_family().
    for nfl_col, our_col in NFLREADPY_KICKER_NATIVE_BUCKETS.items():
        val = row.get(nfl_col)
        if val:
            out[our_col] = val
    for nfl_col, our_col in NFLREADPY_KICKER_DIRECT_COLUMN_MAP.items():
        val = row.get(nfl_col)
        if val:
            out[our_col] = val
    # Flat family (league-2): every miss, blocked kicks included, one bucket.
    fg_missed = _sum_columns(row, FG_MISSED_COLUMNS)
    if fg_missed:
        out["fg_missed"] = fg_missed
    # Banded family (league-1). nflreadpy's six native missed-distance buckets
    # sum exactly to its own fg_missed total (checked over 2025: 140 = 140),
    # so blocked kicks are genuinely NOT in them and are added here by distance.
    banded = {
        "fg_missed_0_39": _sum_columns(row, FG_MISSED_0_39_COLUMNS),
        "fg_missed_40_49": row.get("fg_missed_40_49") or 0,
        "fg_missed_50_59": row.get("fg_missed_50_59") or 0,
        "fg_missed_60_plus": row.get("fg_missed_60_") or 0,
    }
    for distance in _blocked_distances(row):
        if distance < 40:
            banded["fg_missed_0_39"] += 1
        elif distance < 50:
            banded["fg_missed_40_49"] += 1
        elif distance < 60:
            banded["fg_missed_50_59"] += 1
        else:
            banded["fg_missed_60_plus"] += 1
    for key, value in banded.items():
        if value:
            out[key] = value
    pat_missed = _sum_columns(row, PAT_MISSED_COLUMNS)
    if pat_missed:
        out["pat_missed"] = pat_missed
    return out
