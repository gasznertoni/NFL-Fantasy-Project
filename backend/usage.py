"""Usage-trend multiplier (built, rejected, alpha=0). See ARCHITECTURE.md §8."""

from __future__ import annotations

from typing import Any, Optional

from projections import DEFAULT_WINDOW, games_before

USAGE_COLUMNS = ("target_share", "air_yards_share", "wopr")
DEFAULT_USAGE_METRIC = "wopr"
DEFAULT_USAGE_ALPHA = 0.0
DEFAULT_RECENT_GAMES = 2
MIN_BASELINE_USAGE = 0.01
MIN_RATIO = 0.1
MAX_RATIO = 5.0
MIN_USAGE_MULTIPLIER = 0.7
MAX_USAGE_MULTIPLIER = 1.4


def _clean_usage(value: Any) -> Optional[float]:
    """A raw usage value as a float, or None for missing or NaN."""
    if value is None:
        return None
    value = float(value)
    if value != value:  # NaN
        return None
    return value


def _windowed_mean(game_log: list[dict[str, Any]], metric: str, n: int) -> Optional[float]:
    """Mean of `metric` over the last `n` entries, skipping missing values."""
    window = game_log[-n:] if n > 0 else []
    values = [v for v in (_clean_usage(g.get(metric)) for g in window) if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def multiplier_from_usage_ratio(
    ratio: float,
    alpha: float = DEFAULT_USAGE_ALPHA,
    min_ratio: float = MIN_RATIO,
    max_ratio: float = MAX_RATIO,
    lo: float = MIN_USAGE_MULTIPLIER,
    hi: float = MAX_USAGE_MULTIPLIER,
) -> float:
    """Clamp the ratio, raise to alpha, clamp the result."""
    clamped_ratio = max(min_ratio, min(ratio, max_ratio))
    raw = clamped_ratio ** alpha
    return max(lo, min(raw, hi))


def compute_usage_multiplier(
    game_log: list[dict[str, Any]],
    target_season: int,
    target_week: int,
    metric: str = DEFAULT_USAGE_METRIC,
    alpha: float = DEFAULT_USAGE_ALPHA,
    recent_games: int = DEFAULT_RECENT_GAMES,
    baseline_games: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Recent vs trailing usage for one player, as a multiplier, as of target_week."""
    eligible = games_before(game_log, target_season, target_week)
    eligible.sort(key=lambda g: (g["season"], g["week"]))
    games_used = len(eligible)

    baseline_usage = _windowed_mean(eligible, metric, baseline_games)
    recent_usage = _windowed_mean(eligible, metric, recent_games)

    if baseline_usage is None or recent_usage is None or baseline_usage <= MIN_BASELINE_USAGE:
        return {
            "multiplier": 1.0,
            "recent_usage": recent_usage,
            "baseline_usage": baseline_usage,
            "ratio": None,
            "source": "no_usage_data",
            "games_used": games_used,
        }

    ratio = recent_usage / baseline_usage
    return {
        "multiplier": multiplier_from_usage_ratio(ratio, alpha),
        "recent_usage": recent_usage,
        "baseline_usage": baseline_usage,
        "ratio": ratio,
        "source": "current_season",
        "games_used": games_used,
    }


def usage_multipliers_for_pool(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    season: int,
    week: int,
    metric: str = DEFAULT_USAGE_METRIC,
    alpha: float = DEFAULT_USAGE_ALPHA,
    recent_games: int = DEFAULT_RECENT_GAMES,
    baseline_games: int = DEFAULT_WINDOW,
) -> dict[str, float]:
    """{player_id: usage multiplier} for one week."""
    return {
        player_id: compute_usage_multiplier(
            game_log, season, week, metric=metric, alpha=alpha,
            recent_games=recent_games, baseline_games=baseline_games,
        )["multiplier"]
        for player_id, game_log in game_logs_by_player.items()
    }
