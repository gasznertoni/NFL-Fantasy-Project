"""In-house rolling-average projection model. See ARCHITECTURE.md §4."""

from __future__ import annotations

from typing import Any, Optional

from calibration import DEFAULT_SHRINKAGE_K, apply_affine
from scoring import compute_league_points

# Mean of the four measured skill positions only; K's k=30 must not leak in.
_MEASURED_K_POSITIONS = ("QB", "RB", "WR", "TE")
DEFAULT_SHRINKAGE_K_FALLBACK = sum(DEFAULT_SHRINKAGE_K[p] for p in _MEASURED_K_POSITIONS) / len(
    _MEASURED_K_POSITIONS
)

POSITION_CALIBRATION_SCALE: dict[str, float] = {
    "QB": 1.0,
    "RB": 1.0,
    "WR": 1.0,
    "TE": 1.0,
    "DST": 1.0,
    "K": 1.0,
}

DEFAULT_WINDOW = 8
DEFAULT_DECAY = 0.9
DEFAULT_SHRINKAGE_STRENGTH = 0.5

SHRINKAGE_MODES = ("empirical_bayes", "window", "none")
DEFAULT_SHRINKAGE_MODE = "empirical_bayes"


def _rolling_average(per_game_points: list[float], decay: float = DEFAULT_DECAY) -> float:
    """Exponentially decayed average, newest game weight 1.0 (decay=1.0 is the mean)."""
    if not (0 < decay <= 1):
        raise ValueError(f"decay must be in (0, 1], got {decay}")
    if not per_game_points:
        return 0.0
    n = len(per_game_points)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    return sum(w * p for w, p in zip(weights, per_game_points)) / sum(weights)


def games_before(game_log: list[dict[str, Any]], season: int, week: int) -> list[dict[str, Any]]:
    """As-of filter: games strictly before the target week, same season only."""
    return [
        g for g in game_log
        if g["season"] == season and g["week"] < week
    ]


def _confidence(games_used: int, window: int) -> str:
    """Cold-start tag: no_data, low (partial window) or full."""
    if games_used == 0:
        return "no_data"
    if games_used < window:
        return "low"
    return "full"


def _shrinkage_weight(games_used: int, window: int, strength: float) -> float:
    """LEGACY window-mode shrinkage weight, kept only for reproducing old results."""
    if games_used == 0:
        return 0.0
    if games_used >= window:
        return 1.0
    return 1 - strength * (1 - games_used / window)


def _empirical_bayes_weight(games_used: int, k: float) -> float:
    """n / (n + k), with k = sigma2_within / sigma2_between in games."""
    from calibration import empirical_bayes_weight

    return empirical_bayes_weight(games_used, k)


USE_OPPORTUNITY_TD_ESTIMATOR = False


def _estimator_series(
    recent: list[dict[str, Any]], per_game_points: list[float]
) -> list[float]:
    """The per-game series the rolling average is actually taken over."""
    from expected_td import EXPECTED_TD_POINTS_KEY, NON_TD_POINTS_KEY

    out = []
    for game, points in zip(recent, per_game_points):
        non_td = game.get(NON_TD_POINTS_KEY)
        exp_td = game.get(EXPECTED_TD_POINTS_KEY)
        if non_td is None or exp_td is None:
            out.append(points)
            continue
        try:
            value = float(non_td) + float(exp_td)
        except (TypeError, ValueError):
            out.append(points)
            continue
        out.append(points if value != value else value)  # NaN -> real total
    return out


def project_player(
    game_log: list[dict[str, Any]],
    scoring_config: dict[str, Any],
    as_of_season: int,
    as_of_week: int,
    window: int = DEFAULT_WINDOW,
    opponent_multiplier: float = 1.0,
    decay: float = DEFAULT_DECAY,
    positional_baseline: Optional[float] = None,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
    usage_multiplier: float = 1.0,
    calibration_scale: float = 1.0,
    shrinkage_mode: str = DEFAULT_SHRINKAGE_MODE,
    shrinkage_k: Optional[float] = None,
    play_probability: Optional[float] = None,
    affine: Optional[tuple[float, float]] = None,
    interval: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """Project one player's fantasy points for (as_of_season, as_of_week)."""
    if window <= 0:
        raise ValueError(f"window must be a positive integer, got {window}")
    if not (0 <= shrinkage_strength <= 1):
        raise ValueError(f"shrinkage_strength must be in [0, 1], got {shrinkage_strength}")

    eligible = games_before(game_log, as_of_season, as_of_week)
    eligible.sort(key=lambda g: (g["season"], g["week"]))
    recent = eligible[-window:]

    per_game_points = [float(compute_league_points(g, scoring_config)) for g in recent]
    games_used = len(per_game_points)
    rolling_avg = _rolling_average(
        _estimator_series(recent, per_game_points) if USE_OPPORTUNITY_TD_ESTIMATOR
        else per_game_points,
        decay,
    )

    if shrinkage_mode not in SHRINKAGE_MODES:
        raise ValueError(f"shrinkage_mode must be one of {SHRINKAGE_MODES}, got {shrinkage_mode!r}")

    if positional_baseline is None or shrinkage_mode == "none":
        shrinkage_weight = 1.0
        shrunk_avg = rolling_avg
    elif shrinkage_mode == "empirical_bayes":
        k = DEFAULT_SHRINKAGE_K_FALLBACK if shrinkage_k is None else shrinkage_k
        shrinkage_weight = _empirical_bayes_weight(games_used, k)
        shrunk_avg = shrinkage_weight * rolling_avg + (1 - shrinkage_weight) * positional_baseline
    else:
        shrinkage_weight = _shrinkage_weight(games_used, window, shrinkage_strength)
        shrunk_avg = shrinkage_weight * rolling_avg + (1 - shrinkage_weight) * positional_baseline

    conditional = shrunk_avg * opponent_multiplier * usage_multiplier * calibration_scale
    conditional = apply_affine(conditional, affine)

    probability = None if play_probability is None else min(max(float(play_probability), 0.0), 1.0)
    expected = conditional if probability is None else conditional * probability

    low = high = None
    if interval is not None:
        low, high = interval
        if probability is not None:
            low, high = low * probability, high * probability

    return {
        "source": "in_house_estimate",
        "season": as_of_season,
        "week": as_of_week,
        "games_used": games_used,
        "confidence": _confidence(games_used, window),
        "rolling_avg": round(rolling_avg, 2),
        "decay": decay,
        "opponent_multiplier": opponent_multiplier,
        "positional_baseline": positional_baseline,
        "shrinkage_mode": shrinkage_mode,
        "shrinkage_strength": shrinkage_strength,
        "shrinkage_k": shrinkage_k,
        "shrinkage_weight": round(shrinkage_weight, 4),
        "shrunk_avg": round(shrunk_avg, 2),
        "usage_multiplier": usage_multiplier,
        "calibration_scale": calibration_scale,
        "affine": affine,
        "conditional_points": round(conditional, 2),
        "play_probability": None if probability is None else round(probability, 4),
        "projected_points": round(expected, 2),
        "floor": None if low is None else round(low, 2),
        "ceiling": None if high is None else round(high, 2),
        "per_game_points": per_game_points,
    }


def project_players(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    as_of_season: int,
    as_of_week: int,
    window: int = DEFAULT_WINDOW,
    opponent_multipliers: Optional[dict[str, float]] = None,
    decay: float = DEFAULT_DECAY,
    positional_baselines: Optional[dict[str, float]] = None,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
    usage_multipliers: Optional[dict[str, float]] = None,
    calibration_scales: Optional[dict[str, float]] = None,
    shrinkage_mode: str = DEFAULT_SHRINKAGE_MODE,
    shrinkage_ks: Optional[dict[str, float]] = None,
    play_probabilities: Optional[dict[str, float]] = None,
    affines: Optional[dict[str, tuple[float, float]]] = None,
    intervals: Optional[dict[str, tuple[float, float]]] = None,
) -> dict[str, dict[str, Any]]:
    """Batch wrapper over project_player for a slate of players."""
    opponent_multipliers = opponent_multipliers or {}
    positional_baselines = positional_baselines or {}
    usage_multipliers = usage_multipliers or {}
    calibration_scales = calibration_scales or {}
    shrinkage_ks = shrinkage_ks or {}
    play_probabilities = play_probabilities or {}
    affines = affines or {}
    intervals = intervals or {}
    return {
        player_id: project_player(
            game_log,
            scoring_config,
            as_of_season,
            as_of_week,
            window=window,
            opponent_multiplier=opponent_multipliers.get(player_id, 1.0),
            decay=decay,
            positional_baseline=positional_baselines.get(player_id),
            shrinkage_strength=shrinkage_strength,
            usage_multiplier=usage_multipliers.get(player_id, 1.0),
            calibration_scale=calibration_scales.get(player_id, 1.0),
            shrinkage_mode=shrinkage_mode,
            shrinkage_k=shrinkage_ks.get(player_id),
            play_probability=play_probabilities.get(player_id),
            affine=affines.get(player_id),
            interval=intervals.get(player_id),
        )
        for player_id, game_log in game_logs_by_player.items()
    }


def load_recent_games_nflreadpy(player_id: str, season: int, before_week: int) -> list[dict[str, Any]]:
    """Adapter from nflreadpy's load_player_stats() to this module's game_log shape."""
    import nflreadpy as nfl
    from scoring import nflreadpy_row_to_stat_line
    from usage import USAGE_COLUMNS  # local import: avoids a circular import with usage.py

    try:
        stats = nfl.load_player_stats(seasons=[season])
        df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
        df = df[(df["player_id"] == player_id) & (df["week"] < before_week)]

        game_log = []
        for _, row in df.iterrows():
            stat_line = nflreadpy_row_to_stat_line(row.to_dict())
            stat_line["season"] = season
            stat_line["week"] = int(row["week"])
            stat_line["opponent_team"] = row.get("opponent_team")
            stat_line["position"] = row.get("position")
            for col in USAGE_COLUMNS:
                stat_line[col] = row.get(col)
            game_log.append(stat_line)
        return game_log
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_recent_games_nflreadpy: nflreadpy's load_player_stats() "
            "response shape didn't match this adapter's assumptions "
            "(expected columns include 'player_id', 'week', 'opponent_team'). "
            "This adapter was never run against live data -- check "
            "nflreadpy's actual column names for your installed version "
            "and update this function accordingly."
        ) from exc
