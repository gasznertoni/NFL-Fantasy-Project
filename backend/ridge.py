"""Shared ridge, logistic and standardisation helpers. See ARCHITECTURE.md §4."""

from __future__ import annotations

import math
from typing import Any, Optional

DEFAULT_Z_CLIP = 4.0


def num(value: Any) -> Optional[float]:
    """None for anything that isn't a real, finite number -- NaN included."""
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
    """Per-column mean and sd (a constant column gets sd 1.0)."""
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
    """Gauss-Jordan with partial pivoting; singular pivots are skipped."""
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
    """Closed-form ridge with an unpenalised intercept, as [intercept, *coefficients]."""
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
    """L2-penalised logistic regression by IRLS, as [intercept, *coefficients]."""
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
