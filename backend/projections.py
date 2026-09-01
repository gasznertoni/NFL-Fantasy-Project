"""
In-house rolling-average projection model for bench/waiver-tier players --
everyone FantasyPros' free-tier top-10-per-position cap doesn't cover.

Implements docs/design/in-house-projection-model-spec.md sections 3.2-3.3
and 5 (the matchup-difficulty adjustment in section 3.4 is deliberately
deferred, per that doc's own recommendation -- ship the rolling average
alone first). Data loading (nflreadpy) is injected by the caller rather
than called directly from here, so this module is unit-testable with
synthetic game logs and has no network dependency -- see
load_recent_games_nflreadpy() at the bottom for the real adapter, which is
NOT exercised by the test suite (requires network/local nflreadpy).
"""

from __future__ import annotations

from typing import Any, Optional

from scoring import compute_league_points

# Per-position calibration multipliers validated against full 2025 season
# (backend/csv_backtest.py, 5 749 player-week pairs, all 18 weeks).
#
# Findings (2026-09-01):
# - QB: bias_at_scale_1.0 = +0.49 pts/game (weeks 2-18, vs our 6-pt-TD formula).
#   Scaling to the L2-optimal 1.031 marginally worsens MAE (-0.077, p=0.0005)
#   because QB scoring is right-skewed; scale=1.0 is the MAE-minimizing choice.
#   The previous value of 0.85 was set in error (no supporting backtest round)
#   and made underprojection +3.7 pts worse -- corrected here.
# - RB/WR/TE: positive biases (+0.63/+0.45/+0.63) are driven by boom-game
#   outliers inflating mean_actual above median; scaling up statistically
#   significantly worsens MAE for all three. Keep 1.0.
# - CSV note: FantasyPros CSV uses 4-pt passing TDs; our league uses 6-pt.
#   QB calibration used nflreadpy actuals (our formula), not the CSV.
#   See backend/csv_backtest.py and docs/research/projection-model-backtest-findings.md.
POSITION_CALIBRATION_SCALE: dict[str, float] = {
    "QB": 1.0,
    "RB": 1.0,
    "WR": 1.0,
    "TE": 1.0,
    "DST": 1.0,
    "K": 1.0,
}

DEFAULT_WINDOW = 6  # games played -- design spec section 3.2's starting guess was 4;
# raised to 6 per docs/research/projection-model-backtest-findings.md's Round 3
# (2026-08-11, full 611-player pool): window=6 beat window=4 with significance on
# both tuning weeks (p=0.0003) and holdout weeks (p=0.0325), the first time this
# choice cleared a significance bar in either direction.
DEFAULT_DECAY = 1.0  # unweighted mean by default -- see _rolling_average()
DEFAULT_SHRINKAGE_STRENGTH = 0.5  # see _shrinkage_weight(). Set per
# docs/research/projection-model-backtest-findings.md's Round 4: strength=0.5
# gave the smallest |low-tier bias| on tuning weeks and held up on holdout
# (bias magnitude cut by ~70%+ on both week sets, both significant, p=0.0),
# with only a small, guardrail-level pooled MAE cost (+0.09, ~2%). Only
# takes effect when a caller supplies positional_baseline -- with the
# default None, shrinkage never activates regardless of this constant, same
# "inert until wired in" status decay/window had before Round 3/4.
# games_used == 0 (no_data) ignores this constant entirely -- see below.


def _rolling_average(per_game_points: list[float], decay: float = DEFAULT_DECAY) -> float:
    """Exponentially-decayed average over `per_game_points` (chronological
    order, oldest first -- matches project_player's existing sort). The
    most recent game gets weight 1.0; each game further back is multiplied
    by one more factor of `decay`. decay=1.0 (the default) makes every
    weight equal to 1.0, which is arithmetically identical to the plain
    unweighted mean this function replaces -- so the existing default
    behavior (and TestRollingWindow.test_unweighted_mean_not_recency_weighted
    in tests/test_projections.py) is preserved exactly, not approximated.

    Motivated by the bias sign-flip in
    docs/research/projection-model-backtest-findings.md ("Ideas for
    improving accuracy" #1): an unweighted mean lags a player heating up
    or cooling off, since a game from `window` games ago counts exactly as
    much as last week's. Decay < 1.0 discounts older games so the average
    reacts faster to a recent form change -- the tradeoff the findings doc
    flags is reacting to noise (one big or bad game) rather than a real
    trend, which is why decay is swept and paired-tested in backtest.py
    rather than picked by feel.
    """
    if not (0 < decay <= 1):
        raise ValueError(f"decay must be in (0, 1], got {decay}")
    if not per_game_points:
        return 0.0
    n = len(per_game_points)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    return sum(w * p for w, p in zip(weights, per_game_points)) / sum(weights)


def games_before(game_log: list[dict[str, Any]], season: int, week: int) -> list[dict[str, Any]]:
    """As-of filter (design spec sections 3.1 and 3.2): games strictly
    before the target week, same season only. A bye week or missed game
    simply isn't in game_log at all (it's a log of games *played*), so no
    zero-filling is needed here -- the caller's game log is assumed to
    already be "games actually played," not a padded weekly calendar.

    Same-season-only is section 3.2's explicit default ("Don't reach into
    the prior season by default -- a player's role can change completely
    between seasons"), not just a temporal-ordering filter -- a week-1
    projection with no current-season games yet correctly returns zero
    eligible games (cold start), not last season's log.

    Public (not `_`-prefixed): this is the one as-of guarantee every
    module that reads a game_log needs -- projections.py itself,
    baseline.py's positional-baseline aggregation, and usage.py's
    usage-trend calculation all reuse this exact filter rather than each
    reimplementing it slightly differently."""
    return [
        g for g in game_log
        if g["season"] == season and g["week"] < week
    ]


def _confidence(games_used: int, window: int) -> str:
    """Cold-start signal (design spec section 3.2): don't silently average
    over fewer games than the window -- tag it so a report view can show
    "based on 1 game" instead of presenting it with the same visual weight
    as a full window."""
    if games_used == 0:
        return "no_data"
    if games_used < window:
        return "low"
    return "full"


def _shrinkage_weight(games_used: int, window: int, strength: float) -> float:
    """How much weight the player's own rolling average keeps when blended
    toward a positional baseline (see baseline.py) -- `1 - weight` is the
    weight on the baseline. Three cases, matching the three tiers
    _confidence() already defines:

    games_used == 0   -> weight = 0.0   (pure baseline, ignores `strength`
                          entirely -- see rationale below)
    0 < games_used < window -> weight = 1 - strength * (1 - games_used/window)
    games_used >= window    -> weight = 1.0   (the "full" tier is never shrunk)

    Why games_used == 0 is hard-coded rather than strength-scaled like the
    partial-window case: with zero games, `rolling_avg` isn't a thin
    estimate of anything -- it's `_rolling_average([])`'s structural 0.0,
    not data. Weighting a non-observation at all doesn't make sense. This
    split also makes the feature separately ablatable at three settings
    with one knob: strength=0.0 is "fallback-only" (no_data gets the
    baseline, low tier is untouched); strength=1.0 is a full blend of both;
    positional_baseline=None (the caller's choice, not this function's) is
    "off" (see project_player).

    Why `games_used / window` rather than an empirical-Bayes `n / (n + k)`
    form: it's continuous at the tier boundary (no jump right as a
    player's `window`-th game lands), and it reuses the same notion of
    sample completeness _confidence() already uses, rather than
    introducing a second, uncalibrated cutoff `k`.
    """
    if games_used == 0:
        return 0.0
    if games_used >= window:
        return 1.0
    return 1 - strength * (1 - games_used / window)


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
) -> dict[str, Any]:
    """Project one player's fantasy points for (as_of_season, as_of_week).

    Args:
        game_log: list of {"season": int, "week": int, **raw_stat_line}
            dicts for games this player has actually played, in any order.
            Stat-line keys must match scoring_config's category names (see
            scoring.py's NFLREADPY_OFFENSE_COLUMN_MAP if feeding this from
            raw nflreadpy rows).
        scoring_config: passed straight through to compute_league_points --
            same config the FantasyPros tier uses, so both tiers are
            comparable and a config swap updates both at once.
        as_of_season, as_of_week: the week being projected. Only games
            strictly before this are used (section 3.1's as-of discipline)
            -- required parameters, not optional, so leaking future data is
            a type error, not an easy-to-miss default.
        window: trailing games-played window size. Default 4 per the design
            spec's starting guess (section 3.2) -- explicitly flagged there
            as unresearched, worth revisiting once real data exists.
        opponent_multiplier: defense-vs-position adjustment (design spec
            section 3.4). Defaults to 1.0 (no adjustment) since that piece
            is deferred -- passed as a parameter so it's a one-line addition
            later without changing this function's shape.
        decay: exponential recency-decay factor for the rolling average,
            per docs/research/projection-model-backtest-findings.md's
            "ideas for improving accuracy" #1. Must be in (0, 1]. Defaults
            to 1.0 (unweighted mean, i.e. today's behavior) -- see
            _rolling_average()'s docstring for why 1.0 is exactly
            equivalent, not just close, to the old plain-mean code path.
        positional_baseline: a scalar to blend the rolling average toward
            for thin samples -- computed externally by baseline.py (this
            module has zero awareness of positions or the player pool,
            same separation opponent_multiplier/matchup.py already
            established) and injected as a plain float. None (the default)
            means no shrinkage at all: output is arithmetically identical
            to omitting this parameter entirely, matching decay=1.0's
            "exact, not approximate" no-op contract.
        shrinkage_strength: how strongly a partial-window ("low"
            confidence) sample is pulled toward `positional_baseline`; see
            _shrinkage_weight()'s docstring for the exact formula and why
            a zero-game ("no_data") sample ignores this and always takes
            the baseline in full when one is supplied. Must be in [0, 1].
            Only has any effect when positional_baseline is not None.
        usage_multiplier: recent-vs-trailing usage-share trend adjustment
            (usage.py's compute_usage_multiplier -- "idea 3" in
            docs/research/projection-model-backtest-findings.md), same
            "compute externally, inject as a plain float" separation as
            opponent_multiplier. Defaults to 1.0 (no adjustment), applied
            multiplicatively alongside opponent_multiplier -- kept as its
            own named parameter rather than folded into
            opponent_multiplier so a future report view can attribute an
            adjustment to "matchup" vs "usage trend" separately.

    Returns:
        Output contract per design spec section 5: player_id (left to the
        caller to attach -- this function is per-player already),
        season, week, projected_points, games_used, confidence,
        opponent_multiplier, per_game_points (the raw points used to build
        the average, for a report view to show the underlying game log
        next to the projection -- design spec section 3.2's mitigation for
        the role-change-discontinuity failure mode), source (CLAUDE.md's
        "keep both tiers clearly labeled" rule -- a fixed marker so a
        report/eval layer merging this with the FantasyPros tier later can
        tell them apart from the data alone, not just from which function
        produced it), rolling_avg (the pre-shrinkage average -- kept
        separate from shrunk_avg below so the chain rolling_avg ->
        shrunk_avg -> projected_points stays inspectable), positional_baseline
        and shrinkage_strength (echoed, like decay/opponent_multiplier),
        shrinkage_weight (the weight _shrinkage_weight() actually applied --
        1.0 whenever positional_baseline is None), shrunk_avg (the
        post-shrinkage, pre-multiplier average that projected_points is
        actually computed from).

    Raises:
        ValueError: if window is not a positive integer. Python slicing
        makes eligible[-0:] return the *whole* list rather than an empty
        one, so window=0 would otherwise silently mean "no limit" instead
        of "no games" -- reject it outright rather than let that surprise
        a future caller. Also raised if shrinkage_strength is outside
        [0, 1], mirroring _rolling_average()'s decay guard.
    """
    if window <= 0:
        raise ValueError(f"window must be a positive integer, got {window}")
    if not (0 <= shrinkage_strength <= 1):
        raise ValueError(f"shrinkage_strength must be in [0, 1], got {shrinkage_strength}")

    eligible = games_before(game_log, as_of_season, as_of_week)
    eligible.sort(key=lambda g: (g["season"], g["week"]))
    recent = eligible[-window:]

    per_game_points = [float(compute_league_points(g, scoring_config)) for g in recent]
    games_used = len(per_game_points)
    rolling_avg = _rolling_average(per_game_points, decay)

    if positional_baseline is None:
        shrinkage_weight = 1.0
        shrunk_avg = rolling_avg
    else:
        shrinkage_weight = _shrinkage_weight(games_used, window, shrinkage_strength)
        shrunk_avg = shrinkage_weight * rolling_avg + (1 - shrinkage_weight) * positional_baseline

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
        "shrinkage_strength": shrinkage_strength,
        "shrinkage_weight": round(shrinkage_weight, 4),
        "shrunk_avg": round(shrunk_avg, 2),
        "usage_multiplier": usage_multiplier,
        "calibration_scale": calibration_scale,
        "projected_points": round(shrunk_avg * opponent_multiplier * usage_multiplier * calibration_scale, 2),
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
) -> dict[str, dict[str, Any]]:
    """Batch wrapper over project_player for a slate of players. Kept
    separate from project_player so the single-player function stays the
    one unit tests exercise directly and this stays a thin loop.

    positional_baselines: {player_id: float}, exact mirror of
    opponent_multipliers's per-player-id shape -- a player missing from the
    dict (or the dict omitted entirely) gets None, i.e. no shrinkage.
    Position resolution (which baseline a given player_id's position maps
    to) is the caller's job, e.g. baseline.baselines_by_player_week --
    this function, like project_player, has no concept of position.

    usage_multipliers: {player_id: float}, exact mirror of
    opponent_multipliers -- a player missing from the dict (or the dict
    omitted entirely) gets 1.0, i.e. no usage adjustment.

    calibration_scales: {player_id: float} -- per-player position calibration
    multiplier (see POSITION_CALIBRATION_SCALE). Missing → 1.0 (no adjustment).
    Caller builds this from POSITION_CALIBRATION_SCALE keyed by position."""
    opponent_multipliers = opponent_multipliers or {}
    positional_baselines = positional_baselines or {}
    usage_multipliers = usage_multipliers or {}
    calibration_scales = calibration_scales or {}
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
        )
        for player_id, game_log in game_logs_by_player.items()
    }


# ---------------------------------------------------------------------------
# Real data adapter -- NOT exercised by the test suite (network + nflreadpy
# required; confirmed unreachable from the Cowork cloud sandbox, per
# scripts/probe_sources.py's own header note -- run this adapter locally).
# ---------------------------------------------------------------------------
def load_recent_games_nflreadpy(player_id: str, season: int, before_week: int) -> list[dict[str, Any]]:
    """Adapter from nflreadpy's load_player_stats() to this module's
    game_log shape. Intentionally thin and isolated: if nflreadpy's column
    names drift (flagged as a real risk in the design spec section 2),
    only this function needs to change.
    """
    import nflreadpy as nfl  # local import: keeps this an optional/local-only dependency
    from scoring import nflreadpy_row_to_stat_line
    from usage import USAGE_COLUMNS  # local import: avoids a module-level circular
    # import (usage.py imports DEFAULT_WINDOW/games_before from this module)

    try:
        stats = nfl.load_player_stats(seasons=[season])
        df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
        df = df[(df["player_id"] == player_id) & (df["week"] < before_week)]

        game_log = []
        for _, row in df.iterrows():
            stat_line = nflreadpy_row_to_stat_line(row.to_dict())
            stat_line["season"] = season
            stat_line["week"] = int(row["week"])
            # Not a scoring category (ignored by compute_league_points, which
            # only reads keys present in scoring_config) -- carried through so
            # backtest.py can build a matchup-multiplier lookup straight from
            # the game log, no separate roster/schedule join needed.
            stat_line["opponent_team"] = row.get("opponent_team")
            # Also not a scoring category -- carried through so a live caller
            # can resolve this player's position for baseline.py's
            # positional-baseline shrinkage the same way backtest.py's
            # load_full_pool_game_logs already does.
            stat_line["position"] = row.get("position")
            # Also not a scoring category -- usage.py's compute_usage_multiplier
            # reads these directly off the game log, same passthrough pattern.
            for col in USAGE_COLUMNS:
                stat_line[col] = row.get(col)
            game_log.append(stat_line)
        return game_log
    except (KeyError, AttributeError) as exc:
        # Column names/shape here were never confirmed against a live call
        # (design spec section 2 flags this as a real drift risk -- this
        # was built where nflreadpy's data host was unreachable). Re-raise
        # with a pointer at this function instead of a bare pandas
        # KeyError, so whoever runs this first locally isn't left guessing
        # whether it's a real bug or a known nflreadpy-version mismatch.
        raise RuntimeError(
            "load_recent_games_nflreadpy: nflreadpy's load_player_stats() "
            "response shape didn't match this adapter's assumptions "
            "(expected columns include 'player_id', 'week', 'opponent_team'). "
            "This adapter was never run against live data -- check "
            "nflreadpy's actual column names for your installed version "
            "and update this function accordingly."
        ) from exc
