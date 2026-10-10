"""Per-player prior-season shrinkage targets for weeks 2+. See ARCHITECTURE.md §4."""

from __future__ import annotations

import statistics
from typing import Any, Optional

from week1_kdst import shrink

DEFAULT_PRIOR_K = 2.0

COVERED_POSITIONS = ("QB", "RB", "WR", "TE")

FIRST_COVERED_WEEK = 2


def entity_season_means(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    positions: tuple[str, ...] = COVERED_POSITIONS,
) -> dict[str, tuple[float, int, str]]:
    """{player_id: (per-game mean, games, position)} for one completed season."""
    from scoring import compute_league_points

    out: dict[str, tuple[float, int, str]] = {}
    for player_id, games in game_logs_by_player.items():
        position = next((g.get("position") for g in games if g.get("position")), None)
        if position not in positions:
            continue
        points = [float(compute_league_points(game, scoring_config)) for game in games]
        if points:
            out[player_id] = (statistics.mean(points), len(points), position)
    return out


def entity_baselines(
    prior_means: dict[str, tuple[float, int, str]],
    positional_baselines: dict[str, Optional[float]],
    k: float = DEFAULT_PRIOR_K,
) -> dict[str, float]:
    """{player_id: target}: prior-season mean shrunk toward the positional mean."""
    out: dict[str, float] = {}
    for player_id, (mean_points, games, position) in prior_means.items():
        positional = positional_baselines.get(position)
        if positional is None:
            continue
        out[player_id] = round(shrink(mean_points, games, float(positional), k), 4)
    return out


def applies(week: int, position: Optional[str]) -> bool:
    """Whether this module should supply a target for (week, position)."""
    return week >= FIRST_COVERED_WEEK and position in COVERED_POSITIONS


def load_prior_season_logs_nflreadpy(season: int) -> dict[str, list[dict[str, Any]]]:
    """Game logs for `season - 1`, the season the prior comes from."""
    from backtest import load_full_pool_game_logs

    return load_full_pool_game_logs(season - 1, positions=COVERED_POSITIONS)
