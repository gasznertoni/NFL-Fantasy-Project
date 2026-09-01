"""
Small dense linear-algebra kit: ridge regression, logistic regression, and the
imputation/standardisation helpers both of them need.

Why hand-rolled rather than scikit-learn: every fit in this backend is a few
dozen columns over a few hundred to a few thousand rows, solved once per run.
Adding scikit-learn (and its numpy/scipy pin) to requirements.txt for that is a
bad trade in a project whose whole deployment story is "run a script." The
closed forms below are short enough to read against the definitions they
implement.

Extracted from week1.py (2026-09-01) when availability.py and blend.py needed
the same primitives -- week1.py still owns the week-1 feature semantics, this
module owns only the arithmetic.
"""

from __future__ import annotations

import math
from typing import Any, Optional

# A linear model extrapolates without bound, and several callers feed it
# unbounded real-world inputs. Clamping the standardised value keeps a novel
# input "extreme" instead of "arbitrarily far" -- see week1.py's Z_CLIP note for
# the 175.8-point projection that motivated this.
DEFAULT_Z_CLIP = 4.0


def num(value: Any) -> Optional[float]:
    """None for anything that isn't a real, finite number -- NaN included.

    This is the gate every other function here relies on: pandas and polars hand
    back float("nan") rather than None for a missing numeric, and
    `float("nan") is not None` is True, so a plain None-check lets NaN through
    into the arithmetic where it silently poisons a whole fit.
    """
    if isinstance(value, bool):
        return float(value)
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def column_stats(matrix: list[list[float]]) -> tuple[list[float], list[float]]:
    """Per-column mean and sd. A constant column gets sd 1.0 rather than 0, so
    standardising it yields 0 everywhere and the penalty correctly gives it no
    weight, instead of producing a division by zero."""
    n = len(matrix)
    width = len(matrix[0])
    mean = [sum(row[j] for row in matrix) / n for j in range(width)]
    std = []
    for j in range(width):
        var = sum((row[j] - mean[j]) ** 2 for row in matrix) / max(n - 1, 1)
        std.append(math.sqrt(var) if var > 1e-12 else 1.0)
    return mean, std


def standardize(
    vector: list[float], mean: list[float], std: list[float], clip: Optional[float] = None
) -> list[float]:
    z = [(v - m) / s for v, m, s in zip(vector, mean, std)]
    if clip is None:
        return z
    return [max(-clip, min(clip, v)) for v in z]


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan with partial pivoting. A singular pivot is skipped rather
    than raised on: every caller here adds a ridge penalty to the diagonal
    first, so a genuinely singular system means a column carried no information
    and a zero coefficient is the right answer for it."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            continue
        m[col], m[pivot] = m[pivot], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] / pv
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge(design: list[list[float]], targets: list[float], alpha: float) -> list[float]:
    """Closed-form ridge, returned as [intercept, *coefficients].

    Solves (X'X + alpha*I) b = X'y with the intercept left UNPENALISED --
    penalising it would drag the fitted level toward zero, which is a bias, not
    regularisation. alpha > 0 also guarantees a non-singular system even when
    columns are collinear (prior_receptions and prior_targets very much are),
    which is as much the reason for the penalty here as the shrinkage itself.
    """
    n = len(design)
    width = len(design[0])
    x = [[1.0] + row for row in design]
    size = width + 1

    xtx = [[sum(x[i][a] * x[i][b] for i in range(n)) for b in range(size)] for a in range(size)]
    xty = [sum(x[i][a] * targets[i] for i in range(n)) for a in range(size)]
    for j in range(1, size):
        xtx[j][j] += alpha

    return solve(xtx, xty)


def logistic(
    design: list[list[float]],
    targets: list[float],
    l2: float = 1.0,
    max_iter: int = 60,
    tol: float = 1e-8,
) -> list[float]:
    """L2-penalised logistic regression by IRLS (Newton-Raphson on the penalised
    log-likelihood), returned as [intercept, *coefficients].

    Used by availability.py, where the outcome is binary (did the player take
    the field) and a linear probability model would happily predict outside
    [0, 1] for exactly the cases that matter most -- a player ruled Out.

    The intercept is unpenalised, same reasoning as `ridge`. The working weight
    p*(1-p) is floored: once a coefficient drives a fitted probability to
    numerical 0 or 1 -- which happens immediately here, because "Out" means
    P(play) = 0.0006 in the real data -- the unfloored weight is 0 and the
    Hessian goes singular. Divergence is handled by simply stopping: a step
    that fails to solve leaves the last good coefficients in place.
    """
    x = [[1.0] + row for row in design]
    n, size = len(x), len(x[0])
    beta = [0.0] * size

    for _ in range(max_iter):
        p, w = [], []
        for row in x:
            z = sum(b * v for b, v in zip(beta, row))
            z = max(-30.0, min(30.0, z))
            pi = 1.0 / (1.0 + math.exp(-z))
            p.append(pi)
            w.append(max(pi * (1.0 - pi), 1e-6))

        hessian = [
            [sum(x[i][a] * x[i][b] * w[i] for i in range(n)) for b in range(size)]
            for a in range(size)
        ]
        gradient = [sum(x[i][a] * (targets[i] - p[i]) for i in range(n)) for a in range(size)]
        for j in range(1, size):
            hessian[j][j] += l2
            gradient[j] -= l2 * beta[j]

        step = solve(hessian, gradient)
        if not any(math.isfinite(s) for s in step):
            break
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < tol:
            break

    return beta


def predict_linear(beta: list[float], features: list[float]) -> float:
    return beta[0] + sum(b * v for b, v in zip(beta[1:], features))


def predict_probability(beta: list[float], features: list[float]) -> float:
    z = max(-30.0, min(30.0, predict_linear(beta, features)))
    return 1.0 / (1.0 + math.exp(-z))
