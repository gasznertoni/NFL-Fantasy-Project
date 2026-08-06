"""
League scoring engine — turns a raw stat line into league-specific fantasy
points from a configurable scoring_config, instead of trusting any vendor's
precomputed points field.

Why this exists (see CLAUDE.md "Data Sources" and "Notes for agents"):
the builder's real league uses 6-point passing TDs and unusually granular
DST scoring, so a generic STD/PPR/Half-PPR preset misvalues players
(especially QBs). Both projection tiers (FantasyPros top-10/position and
the in-house rolling-average tier) must compute points through this one
function so a config swap is the only change needed once the real league
values land (CLAUDE.md Next Steps item 2) and the four open schema
questions (item 3) are resolved.

Per docs/design/in-house-projection-model-spec.md section 4: this module
is intentionally standalone and unit-tested independently of the
projection logic in projections.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ScoringResult:
    """Points broken down by contribution, so a report view or debug tool
    can show *why* a player scored what they did, not just the total."""

    total: float
    breakdown: dict[str, float]

    def __float__(self) -> float:
        return self.total


def _linear_points(stat_line: dict[str, Any], linear_config: dict[str, float]) -> dict[str, float]:
    """category * value for every category present in both the stat line
    and the config. A category missing from stat_line contributes 0 rather
    than raising -- raw stat sources don't always include every column for
    every player/week (e.g. a QB's stat line has no return-yardage field),
    and treating "absent" as "zero" is the correct behavior here, not an
    error to guard against."""
    breakdown = {}
    for category, value_per_unit in linear_config.items():
        raw = stat_line.get(category)
        if raw:
            breakdown[category] = raw * value_per_unit
    return breakdown


def _milestone_points(stat_line: dict[str, Any], milestone_config: dict[str, dict]) -> dict[str, float]:
    """Yardage milestone bonuses (e.g. 300+/400+ passing). Each category maps
    to {"mode": "highest"|"cumulative", "tiers": [{"threshold", "points"}, ...]}
    -- `mode` lives once per category, not duplicated on every tier entry,
    so a hand-edit to a config file can't leave tiers within the same
    category disagreeing about which mode applies. `tiers` is checked in
    ascending threshold order; "cumulative" sums every threshold met,
    "highest" (the default) applies only the top one met. This is genuinely
    league-configurable and unconfirmed for the real league (CLAUDE.md
    Next Steps item 3c) -- kept as an explicit mode rather than a hardcoded
    assumption so either behavior is a config change, not a code change."""
    breakdown = {}
    for category, spec in milestone_config.items():
        raw = stat_line.get(category)
        tiers = spec.get("tiers") if spec else None
        if not raw or not tiers:
            continue
        mode = spec.get("mode", "highest")
        met = [t for t in tiers if raw >= t["threshold"]]
        if not met:
            continue
        if mode == "cumulative":
            breakdown[f"{category}_milestone"] = sum(t["points"] for t in met)
        else:
            best = max(met, key=lambda t: t["threshold"])
            breakdown[f"{category}_milestone"] = best["points"]
    return breakdown


def _tier_points(stat_line: dict[str, Any], tier_config: dict[str, list]) -> dict[str, float]:
    """Banded scoring for DST points-allowed / yards-allowed: find the
    lowest-`max` band the stat still fits under. Bands must be sorted
    ascending by `max`; the last band should omit `max` (or use a very
    large number) to act as the catch-all worst tier."""
    breakdown = {}
    for category, bands in tier_config.items():
        raw = stat_line.get(category)
        if raw is None or not bands:
            continue
        for band in sorted(bands, key=lambda b: b.get("max", float("inf"))):
            if raw <= band.get("max", float("inf")):
                breakdown[category] = band["points"]
                break
    return breakdown


def compute_league_points(stat_line: dict[str, Any], scoring_config: dict[str, Any]) -> ScoringResult:
    """Compute this league's fantasy points for one player-game (or one
    team-game, for DST) from a raw stat line.

    Args:
        stat_line: raw counting stats, e.g. {"pass_yd": 275, "pass_td": 2,
            "reception": 6, "rec_yd": 80, ...}. Keys are expected to match
            scoring_config's category names -- see
            scoring_config.placeholder.json for the full category list and
            NFLREADPY_COLUMN_MAP below for the nflreadpy column mapping.
        scoring_config: {"linear": {...}, "milestones": {...}, "tiers": {...}}.
            Any of the three top-level keys may be omitted/empty.

    Returns:
        ScoringResult with the total and a per-category breakdown (useful
        for the report view surfacing "why this projection", and for unit
        tests asserting individual category math rather than only the sum).
    """
    breakdown: dict[str, float] = {}
    breakdown.update(_linear_points(stat_line, scoring_config.get("linear", {})))
    breakdown.update(_milestone_points(stat_line, scoring_config.get("milestones", {})))
    breakdown.update(_tier_points(stat_line, scoring_config.get("tiers", {})))
    # round(int, 2) returns an int in Python 3 (e.g. an empty stat_line
    # sums to 0, not 0.0) -- force float first so ScoringResult.total is
    # always a float, never silently an int.
    total = round(float(sum(breakdown.values())), 2)
    return ScoringResult(total=total, breakdown=breakdown)


# ---------------------------------------------------------------------------
# nflreadpy column mapping.
#
# nflreadpy's load_player_stats() columns (per docs/research/phase1-*.md)
# use its own naming (e.g. "passing_yards", "passing_tds"), not this
# module's category names. Kept as an explicit, separately testable mapping
# so a real nflreadpy column-name drift (flagged as a real risk in the
# design spec, section 2) only touches this dict, not compute_league_points
# or its config schema.
#
# NOT exhaustive for DST -- DST needs the self-join/schedule-join logic
# documented in docs/research/dst-scoring-fields.md before yards_allowed /
# points_allowed / block-credit stat lines can be assembled at all; this
# map only covers the offensive skill-position columns confirmed in
# phase1-local-results.json.
# ---------------------------------------------------------------------------
NFLREADPY_OFFENSE_COLUMN_MAP = {
    "passing_yards": "pass_yd",
    "passing_tds": "pass_td",
    "interceptions": "pass_int",
    "passing_2pt_conversions": "pass_2pt",
    "rushing_yards": "rush_yd",
    "rushing_tds": "rush_td",
    "rushing_2pt_conversions": "rush_2pt",
    "receiving_yards": "rec_yd",
    "receiving_tds": "rec_td",
    "receiving_2pt_conversions": "rec_2pt",
    "receptions": "reception",
    "sack_fumbles_lost": "fumble_lost",
    "rushing_fumbles_lost": "fumble_lost",
    "receiving_fumbles_lost": "fumble_lost",
}


def nflreadpy_row_to_stat_line(row: dict[str, Any]) -> dict[str, float]:
    """Map one nflreadpy load_player_stats() row (as a dict) to this
    module's stat_line shape via NFLREADPY_OFFENSE_COLUMN_MAP. fumble_lost
    is summed across nflreadpy's three separate fumble-lost columns (sack/
    rushing/receiving) since this league scores fumbles lost as one flat
    category, not split by how the fumble happened."""
    out: dict[str, float] = {}
    for nfl_col, our_col in NFLREADPY_OFFENSE_COLUMN_MAP.items():
        val = row.get(nfl_col)
        if not val:
            continue
        out[our_col] = out.get(our_col, 0) + val
    return out
