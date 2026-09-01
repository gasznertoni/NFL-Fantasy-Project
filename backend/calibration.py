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
    # DST and K were never in the variance-component study (the audit covered
    # QB/RB/WR/TE only). 1.5 is the midpoint of the four measured values, used
    # so these positions are shrunk *somewhat* rather than not at all -- flagged
    # as an unvalidated default, not a measurement.
    "DST": 1.5,
    "K": 1.5,
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
                    (
                        centre,
                        _quantile(residuals, self.quantiles[0]),
                        _quantile(residuals, self.quantiles[1]),
                    )
                )
            if curve:
                self._fits[position] = curve
        return self

    def interval(self, position: str, projection: float) -> Optional[tuple[float, float]]:
        """(low, high) around `projection`, or None if this position was never
        fitted. The low end is clamped at zero -- a negative floor is
        technically reachable in this league (fumbles and interceptions score
        negative) but it is not a useful thing to show above a lineup slot."""
        curve = self._fits.get(position)
        if not curve:
            return None
        low_off, high_off = _interpolate(curve, float(projection))
        return max(projection + low_off, 0.0), projection + high_off


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


def _interpolate(
    curve: list[tuple[float, float, float]], x: float
) -> tuple[float, float]:
    """Linear interpolation between bucket centres, flat outside the range --
    extrapolating an interval width past the observed data would widen without
    bound for an unusually large projection."""
    if x <= curve[0][0]:
        return curve[0][1], curve[0][2]
    if x >= curve[-1][0]:
        return curve[-1][1], curve[-1][2]
    for (x0, l0, h0), (x1, l1, h1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            span = x1 - x0
            t = 0.0 if span < 1e-9 else (x - x0) / span
            return l0 + t * (l1 - l0), h0 + t * (h1 - h0)
    return curve[-1][1], curve[-1][2]
