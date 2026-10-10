"""Shrinkage k, affine recalibration and prediction intervals. See ARCHITECTURE.md §4."""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from ridge import num

DEFAULT_SHRINKAGE_K: dict[str, float] = {
    "QB": 1.87,
    "RB": 1.11,
    "WR": 1.30,
    "TE": 1.64,
    # D/ST: unvalidated default, never measured.
    "DST": 1.5,
    # K: measured 2026-09-25 (ICC .02-.06). See ARCHITECTURE.md.
    "K": 30.0,
}

MIN_PLAYERS_FOR_VARIANCE = 30
MIN_GAMES_FOR_VARIANCE = 4

MIN_ROWS_FOR_AFFINE = 200
AFFINE_SLOPE_BOUNDS = (0.3, 2.0)

DEFAULT_INTERVAL = (0.10, 0.90)
MIN_ROWS_FOR_INTERVAL = 150

QUANTILE_GRID: tuple[float, ...] = (
    0.01, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50,
    0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99,
)


def variance_components(points_by_player: dict[str, list[float]]) -> Optional[dict[str, float]]:
    """One-way random-effects decomposition of weekly points for one position."""
    groups = [
        [float(p) for p in pts if num(p) is not None]
        for pts in points_by_player.values()
    ]
    groups = [g for g in groups if len(g) >= MIN_GAMES_FOR_VARIANCE]
    if len(groups) < MIN_PLAYERS_FOR_VARIANCE:
        return None

    counts = [len(g) for g in groups]
    means = [sum(g) / len(g) for g in groups]
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
    """{position: k}, falling back to DEFAULT_SHRINKAGE_K on thin data."""
    out = dict(DEFAULT_SHRINKAGE_K)
    for position, by_player in points_by_player_position.items():
        components = variance_components(by_player)
        if components:
            out[position] = components["k"]
    return out


def empirical_bayes_weight(games_used: int, k: float) -> float:
    """`n / (n + k)` -- how much of a player's own sample mean survives."""
    if games_used <= 0:
        return 0.0
    if k <= 0:
        return 1.0
    return games_used / (games_used + k)


def fit_affine(pairs: Iterable[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """Least-squares `actual = a + b * projected`, returned as (a, b)."""
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
    """Apply a fitted (a, b), clamped at zero."""
    if affine is None:
        return max(float(projection), 0.0)
    a, b = affine
    return max(a + b * float(projection), 0.0)


class IntervalModel:
    """Empirical residual quantiles, scaled by the size of the projection."""

    def __init__(
        self,
        quantiles: tuple[float, float] = DEFAULT_INTERVAL,
        buckets: int = 5,
        min_rows: int = MIN_ROWS_FOR_INTERVAL,
    ):
        """min_rows is a parameter because the week-1 model has far fewer rows."""
        self.quantiles = quantiles
        self.buckets = buckets
        self.min_rows = min_rows
        self._fits: dict[str, list[tuple[float, float, float]]] = {}

    def fit(self, rows: Iterable[dict[str, Any]]) -> "IntervalModel":
        """Fit residual quantiles from rows with position, projected and actual."""
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

    def quantile_offset(self, position: str, projection: float, q: float) -> Optional[float]:
        """Offset from `projection` at conditional quantile `q`, or None if unfitted."""
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
        """(low, high) around a conditional projection; mixture band when P(play) given."""
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

        # 1.0 - 0.90 is 0.09999999999999998; the tolerance keeps p=0.90 on the boundary.
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
    """The quantile grid at projection `x`, interpolated between buckets, flat outside."""
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
