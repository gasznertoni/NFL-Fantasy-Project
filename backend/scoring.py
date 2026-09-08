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
from typing import Any, Optional


@dataclass
class ScoringResult:
    """Points broken down by contribution, so a report view or debug tool
    can show *why* a player scored what they did, not just the total."""

    total: float
    breakdown: dict[str, float]

    def __float__(self) -> float:
        return self.total


def resolve_value(value: Any, position: Optional[str]) -> float:
    """Resolve one config value, which may be position-scoped.

    A scoring value is normally a plain number. league-1's real ESPN settings
    price several categories differently by position -- a TE reception is worth
    1.0 where an RB/WR reception is 0.5, and the receiving-yardage milestones
    pay TE > RB/WR > QB -- so a value may instead be a dict of
    {POSITION: number, ..., "default": number}. An unlisted position falls to
    "default"; a dict with no "default" and no match contributes 0, matching
    this module's standing "an absent rule scores nothing" contract.

    Kept public because kicker.py and dst.py validate configs against it."""
    if isinstance(value, dict):
        if position is not None and position in value:
            return value[position]
        return value.get("default", 0)
    return value


def _linear_points(
    stat_line: dict[str, Any],
    linear_config: dict[str, float],
    position: Optional[str] = None,
) -> dict[str, float]:
    """category * value for every category present in both the stat line
    and the config. A category missing from stat_line contributes 0 rather
    than raising -- raw stat sources don't always include every column for
    every player/week (e.g. a QB's stat line has no return-yardage field),
    and treating "absent" as "zero" is the correct behavior here, not an
    error to guard against."""
    breakdown = {}
    for category, value_per_unit in linear_config.items():
        raw = stat_line.get(category)
        if raw and raw == raw:
            points = resolve_value(value_per_unit, position)
            if points:
                breakdown[category] = raw * points
    return breakdown


def _milestone_points(
    stat_line: dict[str, Any],
    milestone_config: dict[str, dict],
    position: Optional[str] = None,
) -> dict[str, float]:
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
            total = sum(resolve_value(t["points"], position) for t in met)
        else:
            best = max(met, key=lambda t: t["threshold"])
            total = resolve_value(best["points"], position)
        if total:
            breakdown[f"{category}_milestone"] = total
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


def compute_league_points(
    stat_line: dict[str, Any],
    scoring_config: dict[str, Any],
    position: Optional[str] = None,
) -> ScoringResult:
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
        position: "QB"/"RB"/"WR"/"TE"/"K"/"DST", needed only where the config
            prices a category by position (see resolve_value). Defaults to
            stat_line["position"], which projections.load_full_pool_game_logs
            and the DST/K assemblers already set on every row they build -- so
            existing two-argument callers keep working unchanged and stay
            correct. Pass it explicitly only to override that.

    Returns:
        ScoringResult with the total and a per-category breakdown (useful
        for the report view surfacing "why this projection", and for unit
        tests asserting individual category math rather than only the sum).
    """
    if position is None:
        position = stat_line.get("position")
    breakdown: dict[str, float] = {}
    breakdown.update(_linear_points(stat_line, scoring_config.get("linear", {}), position))
    breakdown.update(_milestone_points(stat_line, scoring_config.get("milestones", {}), position))
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
    # nflreadpy's column is "passing_interceptions", NOT "interceptions". The
    # latter was mapped here until 2026-09-01 and silently matched nothing, so
    # pass_int's -2 was never applied to any projection or actual: every QB
    # scored ~1.4 pts/game too high (audited over 2023-25, 1 192 unscored INTs
    # across 1 631 starter-games). Confirmed against a live load_player_stats()
    # response -- see docs/research/scoring-engine-audit.md.
    "passing_interceptions": "pass_int",
    "passing_2pt_conversions": "pass_2pt",
    # Added 2026-09-06 with league-1's final ESPN settings, which price the
    # passing line far more finely than any preset: a completion is +0.1, an
    # incompletion -0.1, and a sack taken -0.5. All three were worth nothing
    # here before, and all three are real columns on load_player_stats()
    # (checked against a live 2025 response, per the standing rule that a
    # column-map key matching nothing is silent). Incompletions have no column
    # of their own and are derived below as attempts - completions.
    "completions": "pass_completion",
    "sacks_suffered": "pass_sacked",
    "rushing_first_downs": "rush_first_down",
    "receiving_first_downs": "rec_first_down",
    "rushing_yards": "rush_yd",
    "rushing_tds": "rush_td",
    "rushing_2pt_conversions": "rush_2pt",
    "receiving_yards": "rec_yd",
    "receiving_tds": "rec_td",
    "receiving_2pt_conversions": "rec_2pt",
    "receptions": "reception",
    # Fumbles come from nflreadpy's own whole-player totals rather than the sum
    # of sack_/rushing_/receiving_fumbles(_lost). Those three are a proper
    # SUBSET: over 2023-25 they miss 15.6% of fumbles_total and 10.6% of
    # fumbles_lost_total (a fumble on a return, a lateral, an aborted snap).
    # Summing the parts under-penalised those plays; the totals are exact.
    "fumbles_total": "fumble",
    "fumbles_lost_total": "fumble_lost",
    # Kick/punt return TDs scored by an offensive player. ESPN credits these to
    # the player at the same 6 points as any other TD, and nflreadpy carries
    # them as their own column. Rare (43 across 2023-25, all RB/WR/TE) but a
    # full 6-point swing when they happen, and previously worth 0 to us.
    "special_teams_tds": "return_td",
    # Return yardage accrued by an offensive player. league-1's Miscellaneous
    # block scores kickoff and punt return yards at 1 per 10 (0.1/yd) each.
    # This is the PLAYER side only -- the D/ST-side return-yardage question is
    # a separate one, still resolved as "no such category", see the league-1
    # config's _def_return_yd_removed_2026_09_02 note.
    "kickoff_return_yards": "kick_return_yd",
    "punt_return_yards": "punt_return_yd",
    # A player who recovers a fumble and scores. league-2's real ESPN settings
    # carry this as its own MISC line ("Fumble Recovery TD = 6"); league-1 has
    # no such player-side rule and simply does not define the category, so this
    # contributes nothing there. Column confirmed present in a live
    # load_player_stats() response (2026-09-02).
    "fumble_recovery_tds": "fumble_recovery_td",
}


def nflreadpy_row_to_stat_line(row: dict[str, Any]) -> dict[str, float]:
    """Map one nflreadpy load_player_stats() row (as a dict) to this
    module's stat_line shape via NFLREADPY_OFFENSE_COLUMN_MAP. fumble_lost
    is summed across nflreadpy's three separate fumble-lost columns (sack/
    rushing/receiving) since this league scores fumbles lost as one flat
    category, not split by how the fumble happened. `fumble` (added
    2026-08-16) is summed the same way across the parallel non-"_lost"
    columns -- unconfirmed column names, see the map's own comment."""
    out: dict[str, float] = {}
    position = row.get("position")
    if position is not None and position == position:
        out["position"] = position
    for nfl_col, our_col in NFLREADPY_OFFENSE_COLUMN_MAP.items():
        val = row.get(nfl_col)
        if not val:
            continue
        # `if not val` does NOT catch float("nan") -- NaN is truthy. A NaN here
        # propagates through every sum to a NaN total, and generate_report.py
        # serialises with allow_nan=False, so one bad cell fails the ENTIRE
        # fixture rather than one player. No mapped column carries a NaN in
        # 2024-25, but this is the same trap that produced `"playerId": NaN`
        # in v16, and it costs one comparison to close. Treated as absent,
        # matching the documented "a missing stat contributes 0" contract.
        if val != val:
            continue
        out[our_col] = out.get(our_col, 0) + val
    # Incompletions are not a column -- nflreadpy carries attempts and
    # completions, and league-1 charges -0.1 for the difference. Derived here
    # rather than in the map because it is a subtraction, not a rename. Guarded
    # for NaN on both sides and floored at zero so a bad row can never turn
    # into a positive incompletion credit.
    attempts = row.get("attempts")
    if attempts and attempts == attempts:
        incomplete = attempts - out.get("pass_completion", 0)
        if incomplete > 0:
            out["pass_incompletion"] = incomplete
    return out
