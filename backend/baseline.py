"""Positional baselines for projections.py's shrinkage. See ARCHITECTURE.md §4."""

from __future__ import annotations

import statistics
from typing import Any, Optional

from projections import DEFAULT_WINDOW, games_before
from scoring import compute_league_points

DEFAULT_BASELINE_POPULATION = "thin"
DEFAULT_BASELINE_STATISTIC = "mean"
DEFAULT_DEBUT_MIN_WEEK = 2

_VALID_POPULATIONS = ("thin", "all", "debut")
_VALID_STATISTICS = ("mean", "median")


def _apply_statistic(points: list[float], statistic: str) -> float:
    if statistic not in _VALID_STATISTICS:
        raise ValueError(f"statistic must be one of {_VALID_STATISTICS}, got {statistic!r}")
    if statistic == "median":
        return round(statistics.median(points), 2)
    return round(sum(points) / len(points), 2)


def _player_position(game_log: list[dict[str, Any]]) -> Optional[str]:
    """A player's position, from the first game-log entry that carries one."""
    return next((g.get("position") for g in game_log if g.get("position")), None)


def per_game_points_by_position(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    before_week: Optional[int] = None,
    window: int = DEFAULT_WINDOW,
    population: str = DEFAULT_BASELINE_POPULATION,
    min_week: int = 1,
) -> dict[str, list[float]]:
    """Every eligible player-game's actual points, grouped by position."""
    if population not in _VALID_POPULATIONS:
        raise ValueError(f"population must be one of {_VALID_POPULATIONS}, got {population!r}")

    points_by_position: dict[str, list[float]] = {}
    for game_log in game_logs_by_player.values():
        position = _player_position(game_log)
        if position is None:
            continue

        if before_week is None:
            season_games = sorted((g for g in game_log if g["season"] == season), key=lambda g: g["week"])
        else:
            season_games = sorted(games_before(game_log, season, before_week), key=lambda g: g["week"])
        for games_played_before, g in enumerate(season_games):
            if population == "thin" and games_played_before >= window:
                continue
            if population == "debut" and games_played_before != 0:
                continue
            if g["week"] < min_week:
                continue
            points_by_position.setdefault(position, []).append(float(compute_league_points(g, scoring_config)))

    return points_by_position


def positional_baselines(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    as_of_week: int,
    window: int = DEFAULT_WINDOW,
    population: str = DEFAULT_BASELINE_POPULATION,
    statistic: str = DEFAULT_BASELINE_STATISTIC,
    min_week: int = 1,
) -> dict[str, dict[str, Any]]:
    """Single-week baseline per position, as-of `as_of_week`."""
    if statistic not in _VALID_STATISTICS:
        raise ValueError(f"statistic must be one of {_VALID_STATISTICS}, got {statistic!r}")

    positions = sorted({
        _player_position(log) for log in game_logs_by_player.values() if _player_position(log)
    })

    current_points = per_game_points_by_position(
        game_logs_by_player, scoring_config, season, before_week=as_of_week, window=window, population=population,
        min_week=min_week,
    )

    out: dict[str, dict[str, Any]] = {}
    for position in positions:
        pts = current_points.get(position, [])
        if pts:
            out[position] = {"baseline": _apply_statistic(pts, statistic), "n": len(pts), "source": "current_season"}
            continue

        prior_pts = per_game_points_by_position(
            game_logs_by_player, scoring_config, season - 1, before_week=None, window=window, population=population,
            min_week=min_week,
        ).get(position, [])
        if prior_pts:
            out[position] = {"baseline": _apply_statistic(prior_pts, statistic), "n": len(prior_pts), "source": "prior_season"}
        else:
            out[position] = {"baseline": None, "n": 0, "source": "no_data"}

    return out


def positional_baselines_by_week(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    weeks: list[int],
    window: int = DEFAULT_WINDOW,
    population: str = DEFAULT_BASELINE_POPULATION,
    statistic: str = DEFAULT_BASELINE_STATISTIC,
    min_week: int = 1,
) -> dict[int, dict[str, dict[str, Any]]]:
    """Batch positional_baselines across many weeks, in one chronological pass."""
    if population not in _VALID_POPULATIONS:
        raise ValueError(f"population must be one of {_VALID_POPULATIONS}, got {population!r}")
    if statistic not in _VALID_STATISTICS:
        raise ValueError(f"statistic must be one of {_VALID_STATISTICS}, got {statistic!r}")

    prior_points = per_game_points_by_position(
        game_logs_by_player, scoring_config, season - 1, before_week=None, window=window, population=population,
        min_week=min_week,
    )
    prior_baselines = {
        position: {"baseline": _apply_statistic(pts, statistic), "n": len(pts), "source": "prior_season"}
        for position, pts in prior_points.items()
    }

    entries: list[tuple[int, str, float]] = []
    positions_seen: set[str] = set()
    for game_log in game_logs_by_player.values():
        position = _player_position(game_log)
        if position is None:
            continue
        positions_seen.add(position)

        season_games = sorted((g for g in game_log if g["season"] == season), key=lambda g: g["week"])
        for games_played_before, g in enumerate(season_games):
            if population == "thin" and games_played_before >= window:
                continue
            if population == "debut" and games_played_before != 0:
                continue
            if g["week"] < min_week:
                continue
            entries.append((g["week"], position, float(compute_league_points(g, scoring_config))))
    entries.sort(key=lambda e: e[0])

    result: dict[int, dict[str, dict[str, Any]]] = {}
    accumulator: dict[str, list[float]] = {}
    idx = 0
    for target_week in sorted(weeks):
        while idx < len(entries) and entries[idx][0] < target_week:
            _, position, pts = entries[idx]
            accumulator.setdefault(position, []).append(pts)
            idx += 1

        week_result: dict[str, dict[str, Any]] = {}
        for position in positions_seen:
            current_pts = accumulator.get(position, [])
            if current_pts:
                week_result[position] = {
                    "baseline": _apply_statistic(current_pts, statistic),
                    "n": len(current_pts),
                    "source": "current_season",
                }
            elif position in prior_baselines:
                week_result[position] = prior_baselines[position]
            else:
                week_result[position] = {"baseline": None, "n": 0, "source": "no_data"}
        result[target_week] = week_result

    return result


def baselines_by_player_week(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    baselines_by_week: dict[int, dict[str, dict[str, Any]]],
) -> dict[str, dict[int, float]]:
    """{player_id: {week: baseline}} from each player's position."""
    out: dict[str, dict[int, float]] = {}
    for player_id, game_log in game_logs_by_player.items():
        position = _player_position(game_log)
        if position is None:
            continue
        by_week = {
            week: week_baselines[position]["baseline"]
            for week, week_baselines in baselines_by_week.items()
            if position in week_baselines and week_baselines[position]["baseline"] is not None
        }
        out[player_id] = by_week
    return out


def _games_used_at(game_log: list[dict[str, Any]], season: int, week: int, window: int) -> int:
    """How many games project_player would use for this player at (season, week)."""
    eligible = games_before(game_log, season, week)
    return len(eligible[-window:])


def baselines_by_player_week_for_shrinkage(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    season: int,
    window: int,
    debut_baselines_by_week: dict[int, dict[str, dict[str, Any]]],
    thin_baselines_by_week: dict[int, dict[str, dict[str, Any]]],
) -> dict[str, dict[int, float]]:
    """Per player-week baseline from the debut or thin population, matching games_used."""
    out: dict[str, dict[int, float]] = {}
    for player_id, game_log in game_logs_by_player.items():
        position = _player_position(game_log)
        if position is None:
            continue

        by_week: dict[int, float] = {}
        weeks = set(debut_baselines_by_week) | set(thin_baselines_by_week)
        for week in weeks:
            games_used = _games_used_at(game_log, season, week, window)
            if games_used == 0:
                source_week = debut_baselines_by_week.get(week, {})
            elif games_used < window:
                source_week = thin_baselines_by_week.get(week, {})
            else:
                continue

            entry = source_week.get(position)
            if entry and entry["baseline"] is not None:
                by_week[week] = entry["baseline"]
        out[player_id] = by_week
    return out
