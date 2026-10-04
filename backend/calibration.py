"""
Calibration and uncertainty: turning a raw point estimate into a number whose
magnitude can be trusted, and attaching an interval to it.

Three related pieces, all post-processing of whatever estimator produced the
point estimate:

1. **Variance components** -- decompose weekly fantasy points into
   between-player and within-player variance. The ratio
   `k = sigma2_within / sigma2_between` is the number of games at which a
   player's own sample mean deserves half the weight, and it is what
   projections.py's empirical-Bayes shrinkage needs.

2. **Affine recalibration** -- `a + b * projection`, fitted per position on
   prior seasons. Rank-preserving within a position (b > 0), so it fixes the
   displayed magnitude and cross-position comparability without touching the
   start/sit order.

3. **Uncertainty** -- error spread grows roughly linearly with the projection
   (Breusch-Pagan p from 4e-06 to 2e-110 across the four positions), so a
   single league-wide sd is wrong at both ends. Intervals come from empirical
   residual quantiles rather than a normal, because the outcome distribution is
   strongly right-skewed (skew 1.4-1.6 for RB/WR/TE) and a Gaussian 80%
   interval over-covers at 0.86-0.87.

Why 1 and 2 are both here and both used: they fix different failures. The
empirical-Bayes weight is the principled estimator and it reorders players by
sample size; the affine transform is a pure magnitude fix that provably cannot
change the ranking. See docs/research/scoring-engine-and-model-audit.md
sections 3 and 4.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from ridge import num

# Measured on 2021-2024 (the fallback used when a caller supplies no data to
# estimate from). k = sigma2_within / sigma2_between, in games.
#
#   position   sigma2_between   sigma2_within      k     weight at n=6
#   QB              43.1             80.6        1.87        0.76
#   RB              30.3             33.6        1.11        0.84
#   TE              13.4             22.0        1.64        0.79
#   WR              27.4             35.6        1.30        0.82
#
# Those weights are what the calibration slopes fitted independently on the
# projections came out at (0.735 / 0.878 / 0.841 / 0.852) -- two separate
# routes to the same four numbers, which is the evidence that the
# over-dispersion is regression-to-the-mean and not something else.
DEFAULT_SHRINKAGE_K: dict[str, float] = {
    "QB": 1.87,
    "RB": 1.11,
    "WR": 1.30,
    "TE": 1.64,
    # DST was never in the variance-component study (the audit covered
    # QB/RB/WR/TE only). 1.5 is the midpoint of the four measured values, used
    # so it is shrunk *somewhat* rather than not at all -- flagged as an
    # unvalidated default, not a measurement.
    "DST": 1.5,
    # K is measured (2026-09-25), and it is nothing like the skill positions.
    # Only 2-6% of a kicker's week-to-week variance belongs to the kicker (ICC
    # .021-.056 over 2021-25, against ~.4-.5 for QB/RB/WR/TE), so a couple of
    # games say almost nothing. It used to inherit ~1.5 like DST, which let two
    # PAT-only weeks drag Cameron Dicker (9.4 pts/game in 2025) to 4.25.
    # Leave-one-season-out over 2021-25, weeks 2-18, both leagues: RMSE is flat
    # from ~20 to ~60 with per-fold optima 25-50, and k=30 takes league-1
    # 4.842 -> 4.646 and league-2 4.879 -> 4.679 (p ~1e-12) with pairwise
    # start/sit accuracy unchanged (0.524, a near coin flip in every variant --
    # kicker ranking from kicker history is barely possible at all). A
    # constant, not a run-time fit: the variance-component estimate swings
    # 17-46 by season and is degenerate in 2022, when between-kicker variance
    # rounds to zero. Shrinking toward each kicker's own prior season instead
    # of the kicker mean was measured too and did not help (league-2 pairwise
    # .524 -> .519), so the target stays the positional mean.
    "K": 30.0,
}

# Minimum player-seasons before a fitted k is trusted over the default above.
MIN_PLAYERS_FOR_VARIANCE = 30
# Minimum games in a player-season for it to contribute to the within-player
# variance. Below 4 the sample variance is too noisy to pool usefully.
MIN_GAMES_FOR_VARIANCE = 4

MIN_ROWS_FOR_AFFINE = 200
# An affine fit with a slope this far from 1 is more likely a broken input than
# a real calibration signal; the identity is safer than trusting it.
AFFINE_SLOPE_BOUNDS = (0.3, 2.0)

# Interval quantiles. 10/90 rather than a symmetric sd multiple, because the
# residual distribution is skewed and the two tails are genuinely different
# sizes -- for WR the p5 error is -8.7 and the p95 is +12.3.
DEFAULT_INTERVAL = (0.10, 0.90)
MIN_ROWS_FOR_INTERVAL = 150

# The conditional residual distribution is stored on this grid rather than at
# the two reported quantiles alone. Availability turns the reported band into a
# MIXTURE (zero with probability 1-p, the conditional distribution otherwise),
# and the mixture's 10th/90th percentiles are the conditional distribution's
# quantiles at SHIFTED levels -- see IntervalModel.interval. Those shifted
# levels are not known at fit time, so the whole curve has to be kept.
QUANTILE_GRID: tuple[float, ...] = (
    0.01, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50,
    0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99,
)


# ---------------------------------------------------------------------------
# 1. Variance components
# ---------------------------------------------------------------------------
def variance_components(points_by_player: dict[str, list[float]]) -> Optional[dict[str, float]]:
    """One-way random-effects decomposition of weekly points for one position.

    Args:
        points_by_player: {player_season_key: [points, ...]}. Key by
            (player, season), not player alone -- a player's true level moves
            between seasons, and pooling across them would book that movement
            as within-player noise and inflate k.

    Returns:
        {"within", "between", "k", "icc", "n_players"}, or None if there isn't
        enough data. `icc` is also the ceiling on R-squared for any model that
        knows only player identity: even a projection equal to the player's true
        season mean cannot explain more than this share of weekly variance.
    """
    groups = [
        [float(p) for p in pts if num(p) is not None]
        for pts in points_by_player.values()
    ]
    groups = [g for g in groups if len(g) >= MIN_GAMES_FOR_VARIANCE]
    if len(groups) < MIN_PLAYERS_FOR_VARIANCE:
        return None

    counts = [len(g) for g in groups]
    means = [sum(g) / len(g) for g in groups]
    # Pooled within-group variance (the usual MSE of a one-way ANOVA).
    ss_within = sum(
        sum((v - m) ** 2 for v in g) for g, m in zip(groups, means)
    )
    df_within = sum(counts) - len(groups)
    if df_within <= 0:
        return None
    within = ss_within / df_within

    grand = sum(m * n for m, n in zip(means, counts)) / sum(counts)
    ms_between = sum(n * (m - grand) ** 2 for m, n in zip(means, counts)) / (len(groups) - 1)
    n_bar = sum(counts) / len(counts)
    # The unbiased estimator can go negative when the true between-player
    # variance is near zero; clamp rather than propagate a negative variance.
    between = max((ms_between - within) / n_bar, 1e-6)

    return {
        "within": within,
        "between": between,
        "k": within / between,
        "icc": between / (between + within),
        "n_players": float(len(groups)),
    }


def shrinkage_k_by_position(
    points_by_player_position: dict[str, dict[str, list[float]]]
) -> dict[str, float]:
    """{position: k}, falling back to DEFAULT_SHRINKAGE_K where a position has
    too little data to estimate from."""
    out = dict(DEFAULT_SHRINKAGE_K)
    for position, by_player in points_by_player_position.items():
        components = variance_components(by_player)
        if components:
            out[position] = components["k"]
    return out


def empirical_bayes_weight(games_used: int, k: float) -> float:
    """`n / (n + k)` -- how much of a player's own sample mean survives.

    Replaces projections._shrinkage_weight's `1 - strength * (1 - n/window)`,
    which has no shrinkage at all once `games_used >= window`. That is precisely
    where the measured bias lives: the `full` confidence tier fits a calibration
    slope of 0.55-0.82, meaning a full-window sample mean was still worth only
    about four fifths of its face value and was being taken at par.
    """
    if games_used <= 0:
        return 0.0
    if k <= 0:
        return 1.0
    return games_used / (games_used + k)


# ---------------------------------------------------------------------------
# 2. Affine recalibration
# ---------------------------------------------------------------------------
def fit_affine(pairs: Iterable[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """Least-squares `actual = a + b * projected`, returned as (a, b).

    Returns None when there is too little data, or when the fitted slope lands
    outside AFFINE_SLOPE_BOUNDS -- callers treat None as "apply the identity",
    so a degenerate fit degrades to today's behaviour rather than to a wrong
    number.
    """
    xs, ys = [], []
    for projected, actual in pairs:
        px, py = num(projected), num(actual)
        if px is None or py is None:
            continue
        xs.append(px)
        ys.append(py)
    if len(xs) < MIN_ROWS_FOR_AFFINE:
        return None

    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-9:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    if not (AFFINE_SLOPE_BOUNDS[0] <= slope <= AFFINE_SLOPE_BOUNDS[1]):
        return None
    return my - slope * mx, slope


def apply_affine(projection: float, affine: Optional[tuple[float, float]]) -> float:
    """Apply a fitted (a, b), clamped at zero.

    Rank-preserving whenever b > 0, which fit_affine guarantees via
    AFFINE_SLOPE_BOUNDS -- so this changes the magnitude a reader sees and the
    cross-position comparison a FLEX decision makes, and provably cannot change
    the within-position start/sit order.
    """
    if affine is None:
        return max(float(projection), 0.0)
    a, b = affine
    return max(a + b * float(projection), 0.0)


# ---------------------------------------------------------------------------
# 3. Uncertainty
# ---------------------------------------------------------------------------
class IntervalModel:
    """Empirical residual quantiles, scaled by the size of the projection.

    Residuals are bucketed by projection size and the quantiles of
    `actual - projected` are taken within each bucket, then interpolated. This
    captures both facts the audit established at once: the spread grows with
    the projection, and the two tails are different sizes because the outcome
    distribution is right-skewed.

    Deliberately not a normal interval with a fitted sd: that is easy to write
    and covers 0.86-0.87 at a nominal 80%, because the skew puts more mass in
    the upper tail than a symmetric interval expects.
    """

    def __init__(
        self,
        quantiles: tuple[float, float] = DEFAULT_INTERVAL,
        buckets: int = 5,
        min_rows: int = MIN_ROWS_FOR_INTERVAL,
    ):
        """min_rows is a parameter, not the module constant, because the two
        callers have structurally different sample sizes. The in-season model
        sees 18 weeks a season; the week-1 model sees ONE. Holding week 1 to the
        same 150-row bar silently drops the positions with the smallest pools --
        QB and TE got no interval at all on the first 2026 run for exactly this
        reason, while RB and WR did."""
        self.quantiles = quantiles
        self.buckets = buckets
        self.min_rows = min_rows
        # {position: [(bucket_centre, low_offset, high_offset), ...]}
        self._fits: dict[str, list[tuple[float, float, float]]] = {}

    def fit(self, rows: Iterable[dict[str, Any]]) -> "IntervalModel":
        """rows: dicts with "position", "projected", "actual"."""
        by_pos: dict[str, list[tuple[float, float]]] = {}
        for row in rows:
            p, a = num(row.get("projected")), num(row.get("actual"))
            if p is None or a is None:
                continue
            by_pos.setdefault(row.get("position"), []).append((p, a))

        for position, pairs in by_pos.items():
            if len(pairs) < self.min_rows:
                continue
            pairs.sort(key=lambda t: t[0])
            size = max(len(pairs) // self.buckets, 1)
            curve = []
            for i in range(0, len(pairs), size):
                chunk = pairs[i : i + size]
                if len(chunk) < 20:
                    continue
                centre = sum(p for p, _ in chunk) / len(chunk)
                residuals = sorted(a - p for p, a in chunk)
                curve.append(
                    (centre, tuple(_quantile(residuals, q) for q in QUANTILE_GRID))
                )
            if curve:
                self._fits[position] = curve
        return self

    # -- conditional quantiles ---------------------------------------------
    def quantile_offset(self, position: str, projection: float, q: float) -> Optional[float]:
        """Offset from `projection` at conditional quantile `q`, or None if
        this position was never fitted. Interpolated across the stored grid and
        across bucket centres, flat outside both ranges."""
        curve = self._fits.get(position)
        if not curve:
            return None
        offsets = _interpolate_grid(curve, float(projection))
        return _interp_quantile(QUANTILE_GRID, offsets, float(q))

    def interval(
        self,
        position: str,
        projection: float,
        play_probability: Optional[float] = None,
    ) -> Optional[tuple[float, float]]:
        """(low, high) around `projection`, or None if never fitted.

        `projection` is the CONDITIONAL number -- points if the player suits up
        -- because that is what the residuals were fitted on.

        With `play_probability` supplied, the returned band describes the
        player's ACTUAL outcome, which is a mixture: exactly zero with
        probability `1 - p`, and the conditional distribution otherwise. Its
        quantile function is

            Q(t) = 0                            if t <= 1 - p
                 = Q_cond( (t - (1 - p)) / p )   otherwise

        so the reported 10/90 band becomes

            floor   = 0                   if p <= 0.90  else Q_cond(1 - 0.90/p)
            ceiling = Q_cond(1 - 0.10/p)  if p >  0.10  else 0

        This replaces `lo, hi = lo * p, hi * p`, which was neither of those
        things. Scaling the conditional quantiles shrinks the ceiling toward
        zero while holding the floor away from it -- both wrong, in opposite
        directions. Measured coverage of the old band against a nominal 80%
        fell to 64% at p=0.85 and 26.5% at p=0.50; 88% of the bands published
        in the live week-1 report sat below p=0.90, where the floor should be
        exactly zero. See docs/research/second-audit-2026-09-02.md section 2.

        The low end is clamped at zero: a negative floor is reachable in these
        leagues (interceptions and fumbles score negative) but is not a useful
        thing to show above a lineup slot.
        """
        curve = self._fits.get(position)
        if not curve:
            return None
        lo_q, hi_q = self.quantiles

        if play_probability is None:
            low_off = self.quantile_offset(position, projection, lo_q)
            high_off = self.quantile_offset(position, projection, hi_q)
            return max(projection + low_off, 0.0), projection + high_off

        p = max(0.0, min(1.0, float(play_probability)))
        miss = 1.0 - p
        if p <= 0.0:
            return 0.0, 0.0

        # The mixture has an atom at zero covering the whole level range
        # [0, 1-p], so a requested level AT the boundary reads zero too. The
        # tolerance matters at the exact values this is called with: 1.0 - 0.90
        # is 0.09999999999999998, so a bare `lo_q <= miss` misses p=0.90 --
        # precisely the boundary case the reported band turns on.
        eps = 1e-9
        if lo_q <= miss + eps:
            low = 0.0
        else:
            off = self.quantile_offset(position, projection, (lo_q - miss) / p)
            low = max(projection + off, 0.0)
        if hi_q <= miss + eps:
            high = 0.0
        else:
            off = self.quantile_offset(position, projection, (hi_q - miss) / p)
            high = max(projection + off, 0.0)
        return low, max(high, low)


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _interpolate_grid(
    curve: list[tuple[float, tuple[float, ...]]], x: float
) -> tuple[float, ...]:
    """The whole quantile grid at projection `x`, linearly interpolated between
    bucket centres and flat outside the observed range -- extrapolating an
    interval width past the data would widen without bound for an unusually
    large projection."""
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for (x0, g0), (x1, g1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            span = x1 - x0
            t = 0.0 if span < 1e-9 else (x - x0) / span
            return tuple(a + t * (b - a) for a, b in zip(g0, g1))
    return curve[-1][1]


def _interp_quantile(
    grid: tuple[float, ...], offsets: tuple[float, ...], q: float
) -> float:
    """Linear interpolation along the quantile axis, clamped to the grid ends."""
    if q <= grid[0]:
        return offsets[0]
    if q >= grid[-1]:
        return offsets[-1]
    for i in range(len(grid) - 1):
        if grid[i] <= q <= grid[i + 1]:
            span = grid[i + 1] - grid[i]
            t = 0.0 if span < 1e-12 else (q - grid[i]) / span
            return offsets[i] + t * (offsets[i + 1] - offsets[i])
    return offsets[-1]
