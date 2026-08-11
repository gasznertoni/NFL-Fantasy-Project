"""
Usage/opportunity-trend adjustment for projections.py -- "idea 3" from
docs/research/projection-model-backtest-findings.md, flagged there as the
highest-leverage untested idea: `nflreadpy`'s player-stats table already
carries `target_share`/`air_yards_share`/`wopr` (usage share, a process
signal) and this model doesn't touch any of it, using only points (a
noisier outcome signal). Same "compute externally, inject as a plain
float" pattern matchup.py/baseline.py already established --
project_player() has zero awareness of this module, just a
`usage_multiplier` parameter it multiplies in when one is supplied.

Signal shape: a trend-based multiplier (recent usage vs. this player's own
trailing baseline usage), not a usage-to-points regression. A regression
needs a fitted, per-position, refit-each-week mapping -- a real research
project this repo deliberately doesn't take on (no numpy/scipy dependency,
see backtest.py's own stdlib-only precedent). A ratio is self-normalizing
per player and needs no cross-player calibration, and it directly targets
Round 3's finding: the "low"-confidence tier's bias skews toward
role-emerging players (call-ups, players who just won a job) -- exactly
the pattern a rising usage-share trend would catch before the points do.

`wopr` (`1.5*target_share + 0.7*air_yards_share`) is the default metric --
a composite of volume and depth, the standard opportunity metric --
`target_share` alone is available via the `metric` parameter as a sweep
comparator. `DEFAULT_USAGE_METRIC`/`USAGE_COLUMNS` are used by
`backtest.load_full_pool_game_logs` and `projections.load_recent_games_nflreadpy`
to carry these columns through as non-scoring extra keys, the same way
`position`/`opponent_team` already are -- NOT via scoring.py, whose
`nflreadpy_row_to_stat_line` drops falsy values (would silently discard a
real `target_share == 0.0`) and whose contract is scoring-categories-only.

Two things confirmed against live 2025 data before picking any constant
below (a read-only probe, matching the discipline baseline.py's two
backtest-confound fixes established -- check real data before trusting a
formula):

1. `wopr` can be NEGATIVE (`air_yards_share`'s denominator/numerator can
   go net-negative on a low-volume/garbage-time week; observed min ~-9 in
   the real 2025 pool). `multiplier_from_usage_ratio` clamps the RATIO to
   `[MIN_RATIO, MAX_RATIO]` (both positive) BEFORE raising it to `alpha` --
   raising a negative base to a non-integer exponent is a domain error in
   Python, so clamping only the final multiplier (the original design
   sketch) would still crash upstream of that clamp on a negative input.
2. The recent-vs-baseline ratio's real distribution is wide (observed
   p10=0.45, p90=1.58, median~0.98) -- notably wider than an initial guess
   of `[0.85, 1.20]` for the *output* multiplier bound. `MIN_USAGE_MULTIPLIER`/
   `MAX_USAGE_MULTIPLIER` below are set from that observed spread, not a
   round-number guess.

QB / no-signal gating is data-driven (`baseline_usage <= MIN_BASELINE_USAGE`
-> neutral), not a position allow-list -- confirmed QBs have `wopr` present
but ~always 0 (they essentially never catch passes), so this one guard
handles QBs, a zero-target receiver, and an unstable tiny-denominator ratio
all at once, and stays correct if K/DST are ever added without a list
update. RB scope for this round is receiving usage only -- an RB's
dominant usage signal is carry share, which needs a separate team-level
`carries` aggregation (feasible, real follow-up work, not built here).
"""

from __future__ import annotations

from typing import Any, Optional

from projections import DEFAULT_WINDOW, games_before

USAGE_COLUMNS = ("target_share", "air_yards_share", "wopr")
DEFAULT_USAGE_METRIC = "wopr"
DEFAULT_USAGE_ALPHA = 0.0  # alpha=0 -> multiplier is always exactly 1.0,
# regardless of ratio -- the same "default reproduces today's behavior
# exactly" contract decay=1.0/opponent_multiplier=1.0 already have.
DEFAULT_RECENT_GAMES = 2
MIN_BASELINE_USAGE = 0.01  # at/below this, treat as no signal (covers QBs,
# zero-target receivers, and an unstable near-zero-denominator ratio alike)
MIN_RATIO = 0.1  # floor applied to the ratio BEFORE exponentiating -- see
MAX_RATIO = 5.0  # module docstring point 1 (avoids a domain error on a
# negative or near-zero ratio; ceiling matches the observed p99 in real data)
MIN_USAGE_MULTIPLIER = 0.7  # output clamp -- see module docstring point 2
MAX_USAGE_MULTIPLIER = 1.4


def _clean_usage(value: Any) -> Optional[float]:
    """Normalizes a raw usage-column value to a float or None. Handles
    both an outright missing value (None) and pandas' float NaN (which is
    truthy and fails every comparison, so `value != value` -- the
    self-inequality trick -- is the only reliable NaN check without a
    pandas import here)."""
    if value is None:
        return None
    value = float(value)
    if value != value:  # NaN
        return None
    return value


def _windowed_mean(game_log: list[dict[str, Any]], metric: str, n: int) -> Optional[float]:
    """Mean of `metric` over the last `n` entries of `game_log` (already
    as-of-filtered and chronologically sorted by the caller), skipping any
    individual entry with a missing/NaN value rather than requiring the
    whole window to be clean. None if nothing usable remains."""
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
    """ratio -> bounded multiplier: clamp the ratio to a safe positive
    range first (module docstring point 1), then `ratio ** alpha`, then
    clamp the result to `[lo, hi]`. `alpha=0` returns exactly `1.0` for
    any input, since `clamped_ratio ** 0 == 1.0` for any positive base --
    the clamp guarantees the base is always positive, so this holds even
    for a negative or zero raw ratio."""
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
    """Compare a player's recent `metric` usage to their own trailing
    baseline, as-of `target_week` (same discipline as
    projections.games_before -- only games strictly before this week).

    Args:
        metric: which USAGE_COLUMNS entry to read. Default "wopr".
        alpha: exponent applied to the ratio -- see
            multiplier_from_usage_ratio. 0 means no adjustment.
        recent_games: how many of the most recent eligible games count as
            "recent." Must be <= baseline_games for "recent" to be a
            subset of "baseline" (both slice the same as-of-filtered,
            chronologically sorted game log) -- not enforced, but a
            recent_games > baseline_games call would compare overlapping
            but non-nested windows, which isn't the intended comparison.
        baseline_games: trailing window size for the player's own
            "normal" usage level. Defaults to DEFAULT_WINDOW, matching the
            points-average's own window so both signals span the same
            horizon.

    Returns:
        {"multiplier", "recent_usage", "baseline_usage", "ratio",
        "source", "games_used"}. `source` is "current_season" (a real
        ratio was computed) or "no_usage_data" (baseline usage missing or
        <= MIN_BASELINE_USAGE -- multiplier is 1.0, recent_usage/
        baseline_usage/ratio may be partially populated for
        inspection but should not be trusted as a real signal).
        `games_used` is the same as project_player's for this game_log/
        week (all eligible games, not just the recent/baseline windows) --
        useful for a report view to show alongside the points projection.
    """
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
    """Batch wrapper for a single week -- {player_id: multiplier}, the
    shape project_players' usage_multipliers parameter wants. Unlike
    baseline.py's positional baselines, usage comes entirely from each
    player's own log, so there's no pool-level aggregation here -- this is
    a thin loop, kept for the same live-orchestration convenience
    baseline.py's batch functions provide, not because it's expensive to
    inline."""
    return {
        player_id: compute_usage_multiplier(
            game_log, season, week, metric=metric, alpha=alpha,
            recent_games=recent_games, baseline_games=baseline_games,
        )["multiplier"]
        for player_id, game_log in game_logs_by_player.items()
    }
