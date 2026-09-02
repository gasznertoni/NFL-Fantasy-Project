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

from calibration import DEFAULT_SHRINKAGE_K, apply_affine
from scoring import compute_league_points

# Used when a caller asks for empirical-Bayes shrinkage without supplying a
# per-position k. The mean of the four measured values -- a caller that knows
# the player's position should pass calibration.DEFAULT_SHRINKAGE_K[position]
# (or a freshly fitted k) instead of relying on this.
DEFAULT_SHRINKAGE_K_FALLBACK = sum(DEFAULT_SHRINKAGE_K.values()) / len(DEFAULT_SHRINKAGE_K)

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

DEFAULT_WINDOW = 8  # games played.
# Raised from 6 to 8 on 2026-09-01 (docs/research/scoring-engine-and-model-audit.md
# section 2). The earlier Round-3 sweep compared 4 against 6, found 6 better, and
# stopped -- it never tested past 6, where the improvement keeps going. Sweeping
# window x decay over 2023-25 and scoring on BOTH objectives at once:
#
#   window  decay    MAE     RMSE   pairwise acc   Spearman rho
#     12     0.9    4.715   6.762      0.7348         0.663
#      8     0.9    4.726   6.785      0.7344         0.662
#      6     1.0    4.755   6.833      0.7325         0.659   <- previous default
#      4     1.0    4.834   6.934      0.7294         0.654
#
# 12 and 8 are within noise of each other and both beat 6 on all four metrics
# simultaneously. 8 is chosen over 12 because a shorter window reacts faster to
# a real role change (the failure mode the design spec's section 3.2 cares
# about) at essentially no measured cost.
DEFAULT_DECAY = 0.9  # exponential recency weight -- see _rolling_average().
# Also changed 2026-09-01, and the two changes belong together: a longer window
# only helps when the older games in it are discounted. Every decay=0.9 row in
# the sweep above beats its decay=1.0 counterpart at the same window.
DEFAULT_SHRINKAGE_STRENGTH = 0.5  # legacy "window" shrinkage mode only -- see
# _shrinkage_weight() and DEFAULT_SHRINKAGE_MODE below. Kept at Round 4's value
# so shrinkage_mode="window" reproduces the old behaviour exactly.

# Empirical-Bayes is the default as of 2026-09-01. The old "window" mode applies
# no shrinkage at all once games_used >= window, and that is precisely where the
# measured bias lives: the `full` confidence tier fits a calibration slope of
# 0.55-0.82, i.e. a full-window sample mean was worth about four fifths of its
# face value and was being taken at par. Every position's projections were
# over-dispersed with p < 1e-11 (audit section 3).
SHRINKAGE_MODES = ("empirical_bayes", "window", "none")
DEFAULT_SHRINKAGE_MODE = "empirical_bayes"


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
    """LEGACY `shrinkage_mode="window"` weight -- kept so the pre-2026-09-01
    behaviour is reproducible for comparison, not because it is recommended.

    games_used == 0        -> 0.0   (pure baseline)
    0 < games_used < window -> 1 - strength * (1 - games_used/window)
    games_used >= window    -> 1.0  (no shrinkage at all)

    That last line is the defect. A `window`-game sample mean is not worth its
    face value, and taking it at par is what makes the projections
    over-dispersed: calibration slopes of 0.735-0.878 across the four positions,
    every one rejecting b = 1 at p < 1e-11. Use
    `shrinkage_mode="empirical_bayes"` (the default) instead, which shrinks at
    every sample size by the amount the variance components say it should.
    """
    if games_used == 0:
        return 0.0
    if games_used >= window:
        return 1.0
    return 1 - strength * (1 - games_used / window)


def _empirical_bayes_weight(games_used: int, k: float) -> float:
    """`n / (n + k)`, with k = sigma2_within / sigma2_between in games.

    The weight a player's own sample mean earns given how much of the
    week-to-week spread is signal rather than noise. Derived rather than tuned:
    calibration.variance_components estimates k directly from game logs, and at
    the old window of 6 it reproduces the independently-fitted calibration
    slopes almost exactly (theory 0.76/0.84/0.79/0.82 against measured
    0.735/0.878/0.841/0.852). Two different routes to the same four numbers is
    the evidence that this bias is regression to the mean, and that this is the
    correct correction for it.

    Continuous in `games_used` with no cliff at the window boundary, which the
    legacy mode's tier switch could not offer.
    """
    from calibration import empirical_bayes_weight

    return empirical_bayes_weight(games_used, k)


# Built, tested, and deliberately LEFT OFF -- the same call matchup.py and
# usage.py already carry, for the same reason.
#
# The idea is sound and the standalone measurement was real: substituting an
# opportunity-based touchdown term for the player's own touchdown history beat
# the plain rolling average at every position (RB p=1.2e-02, WR p=6.4e-03,
# TE p=3.6e-03) and was worth +0.48 lineup points a week in a 1 920-lineup
# simulation. That is what the second audit reported.
#
# It does not survive integration. Re-measured through the REAL pipeline --
# shrinkage, affine recalibration, and a blend that already carries rolling
# volume and the Vegas implied total -- the gain collapses to nothing:
#
#   league-1, on top of snap share:  2025  6.4872 -> 6.4763  p=0.170
#                                    2024  6.5569 -> 6.5596  p=0.687  (worse)
#   league-2, on top of snap share:  2025  6.1662 -> 6.1579  p=0.135
#                                    2024  6.2659 -> 6.2698  p=0.387  (worse)
#
# Not significant in either direction, and the SIGN FLIPS between held-out
# seasons -- the signature of noise, not signal. The volume block already
# carries most of what opportunity-based touchdowns know, so the marginal value
# against the full stack is nil even though it is real against a bare average.
#
# Kept rather than deleted because it is validated, tested code and a future
# estimator without the volume blend would want it. Flip to True to enable.
# See docs/research/second-audit-2026-09-02.md section 4 for both measurements
# and why they disagree.
USE_OPPORTUNITY_TD_ESTIMATOR = False


def _estimator_series(
    recent: list[dict[str, Any]], per_game_points: list[float]
) -> list[float]:
    """The per-game series the rolling average is actually taken over.

    Fantasy points decompose exactly as `non-TD points + TD points`, and the two
    halves behave nothing alike: touchdowns are the least stable component in
    the system (receiving-TD ICC 0.07-0.11) while yardage and volume are
    comparatively steady. Where expected_td.py has attached both halves to a
    game, this returns

        non_td_points + exp_td_points

    -- the player's own stable production, plus what his OPPORTUNITY says the
    touchdowns were worth, instead of the touchdowns he happened to score.

    Measured held-out on 2024-25, this beats the plain rolling total at every
    position (RB p=1.2e-02, WR p=6.4e-03, TE p=3.6e-03) and is worth +0.48
    lineup points a week, t=2.86, p=4.2e-03. See
    docs/research/second-audit-2026-09-02.md section 4.

    Why here rather than as a blend feature: it was measured as a SUBSTITUTION
    for the rolling total, and adding the two halves alongside `rolling_avg` in
    blend.py instead is worth nothing (p=0.60 on held-out 2025 through the real
    pipeline). The ridge cannot exploit a decomposition of a feature it is
    already given whole. An augmentation and a substitution are not the same
    change, and only the substitution reproduces the measurement.

    Falls back per game, not per player: a game missing either half keeps its
    real total, so ~10% expected-TD coverage gaps degrade one week rather than
    disqualifying a player's whole history.
    """
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
    # The ESTIMATOR series may differ from the DISPLAY series. per_game_points
    # is what the report shows behind a projection, so it stays the player's
    # real weekly scores. The average is taken over _estimator_series(), which
    # substitutes an opportunity-based touchdown term where one is available.
    rolling_avg = _rolling_average(
        _estimator_series(recent, per_game_points) if USE_OPPORTUNITY_TD_ESTIMATOR
        else per_game_points,
        decay,
    )

    if shrinkage_mode not in SHRINKAGE_MODES:
        raise ValueError(f"shrinkage_mode must be one of {SHRINKAGE_MODES}, got {shrinkage_mode!r}")

    # Shrinkage needs something to shrink TOWARD. With no positional_baseline
    # supplied there is no target, so every mode degrades to "no shrinkage" --
    # the same "off means arithmetically identical" contract the other optional
    # parameters keep.
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
    # Rank-preserving magnitude correction (calibration.apply_affine clamps at
    # zero and is only ever fitted with a positive slope), applied BEFORE the
    # availability multiplier so the affine fit stays a statement about the
    # points estimate rather than about injury risk.
    conditional = apply_affine(conditional, affine)

    # E[points] = P(play) x E[points | play]. The (1 - P) branch contributes
    # exactly zero, not approximately: a player who does not take the field
    # scores nothing. This is the number a start/sit comparison should use --
    # see availability.py's module docstring for why it matters more than every
    # estimator knob in this file combined.
    probability = None if play_probability is None else min(max(float(play_probability), 0.0), 1.0)
    expected = conditional if probability is None else conditional * probability

    low = high = None
    if interval is not None:
        low, high = interval
        if probability is not None:
            # Scale the interval with the point estimate so the two stay
            # consistent. The floor is NOT scaled below zero-if-he-sits: an
            # unavailable player's realistic low is 0, which multiplying the
            # conditional floor by P(play) already approaches.
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
        # The "if he plays" number, kept separate from the expected value so a
        # report view can show both and the eval layer can score each against
        # the right ground truth.
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
