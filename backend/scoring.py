"""League scoring engine: raw stat line -> league fantasy points. See ARCHITECTURE.md §3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ScoringResult:
    """Total points plus the per-category breakdown behind them."""

    total: float
    breakdown: dict[str, float]

    def __float__(self) -> float:
        return self.total


def resolve_value(value: Any, position: Optional[str]) -> float:
    """Resolve one config value, which may be position-scoped."""
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
    """category * value for each category in both the stat line and the config."""
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
    """Yardage milestone bonuses, in highest or cumulative mode."""
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
    """Banded D/ST scoring: the lowest-`max` band the value fits under."""
    breakdown = {}
    for category, bands in tier_config.items():
        raw = stat_line.get(category)
        # NaN is neither None nor <= any band; treat it as missing.
        if raw is None or raw != raw or not bands:
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
    """This league's fantasy points for one player-game or team-game."""
    if position is None:
        position = stat_line.get("position")
    breakdown: dict[str, float] = {}
    breakdown.update(_linear_points(stat_line, scoring_config.get("linear", {}), position))
    breakdown.update(_milestone_points(stat_line, scoring_config.get("milestones", {}), position))
    breakdown.update(_tier_points(stat_line, scoring_config.get("tiers", {})))
    total = round(float(sum(breakdown.values())), 2)
    return ScoringResult(total=total, breakdown=breakdown)


NFLREADPY_OFFENSE_COLUMN_MAP = {
    "passing_yards": "pass_yd",
    "passing_tds": "pass_td",
    # The column is passing_interceptions; a key that matches no column scores 0 silently.
    "passing_interceptions": "pass_int",
    "passing_2pt_conversions": "pass_2pt",
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
    "fumbles_total": "fumble",
    "fumbles_lost_total": "fumble_lost",
    "special_teams_tds": "return_td",
    "kickoff_return_yards": "kick_return_yd",
    "punt_return_yards": "punt_return_yd",
    "fumble_recovery_tds": "fumble_recovery_td",
}


def nflreadpy_row_to_stat_line(row: dict[str, Any]) -> dict[str, float]:
    """Map one load_player_stats() row to this module's stat-line keys."""
    out: dict[str, float] = {}
    position = row.get("position")
    if position is not None and position == position:
        out["position"] = position
    for nfl_col, our_col in NFLREADPY_OFFENSE_COLUMN_MAP.items():
        val = row.get(nfl_col)
        if not val:
            continue
        # NaN is truthy, so `if not val` misses it. Treat NaN as absent.
        if val != val:
            continue
        out[our_col] = out.get(our_col, 0) + val
    attempts = row.get("attempts")
    if attempts and attempts == attempts:
        incomplete = attempts - out.get("pass_completion", 0)
        if incomplete > 0:
            out["pass_incompletion"] = incomplete
    return out
