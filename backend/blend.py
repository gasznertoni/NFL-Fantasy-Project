"""
In-season blend: a small per-position ridge that refines the rolling average
with rolling *volume* and the week's game context.

Why volume. Fantasy points are a noisy function of a much more stable
underlying quantity. Intraclass correlation of the input itself, measured over
2021-2025:

    position   fantasy points   primary volume stat   target share
    WR              0.43        targets   0.56            0.61
    RB              0.46        carries   0.60            0.44
    TE              0.37        targets   0.51            0.54
    QB              0.25        attempts  0.32             --

Touchdowns are the least stable component of all -- receiving-TD ICC is
0.07-0.11 -- and they are most of the week-to-week noise. Averaging *points*
inherits that noise directly; averaging volume and letting a fitted coefficient
convert it to points keeps the stable part and regresses the unstable part.

Measured on held-out 2025 (RMSE), against the rolling average alone:

    position   rolling only   + volume   + volume + context
    QB            10.382       10.364          10.249
    RB             6.676        6.630           6.585
    WR             6.029        5.965           5.968
    TE             5.430        5.351           5.337

Modest -- 1.5-3% -- and honestly reported as such. It is recommendation 5 of 7
in docs/research/scoring-engine-and-model-audit.md, well behind availability.

Architecture matches week1.py: pure model math, injectable rows, no network.
The rolling-average feature is produced by projections.py and passed in, so
this module has no opinion about how the base estimate is built and can be
ablated by simply not calling it.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ridge import DEFAULT_Z_CLIP, column_stats, median, num, predict_linear, ridge, standardize

# Rolling volume/usage columns, averaged over the same trailing window as the
# points average. Names match nflreadpy's load_player_stats() columns so the
# adapter is a straight passthrough.
VOLUME_COLUMNS: tuple[str, ...] = (
    "targets",
    "carries",
    "attempts",
    "receptions",
    "target_share",
    "wopr",
    "receiving_air_yards",
    "receiving_yards",
    "rushing_yards",
    "passing_yards",
    # Snap share, added 2026-09-02 by the second audit. Comes off the game log
    # like every other column here, so rolling_volume averages it under the same
    # window and decay as the points average. Consistent RMSE gain at all four
    # positions, and confirmed through the real pipeline on held-out 2025:
    # 6.5089 -> 6.4872 for league-1 (p=1.4e-03), 6.1877 -> 6.1662 for league-2
    # (p=1.2e-03), with pairwise accuracy up in both.
    #
    # The TD/non-TD split from expected_td.py is deliberately NOT here. It was
    # tried as two extra features and is worth nothing that way (p=0.60): the
    # ridge cannot exploit a decomposition of `rolling_avg`, which it is already
    # given whole. It lives in projections._estimator_series instead, as the
    # substitution it was actually measured as.
    #
    # See docs/research/second-audit-2026-09-02.md sections 4 and 6.
    "offense_pct",
)

CONTEXT_COLUMNS: tuple[str, ...] = (
    "implied_team_total",
    "total_line",
    "spread_line",
    "is_home",
)

# The base estimate from projections.py, plus how much evidence is behind it.
BASE_COLUMNS: tuple[str, ...] = ("rolling_avg", "shrunk_avg", "games_used")

FEATURES: tuple[str, ...] = BASE_COLUMNS + VOLUME_COLUMNS + CONTEXT_COLUMNS

# Swept on held-out 2025 across 3/10/30/100/300. Flat between 10 and 100;
# 30 sits in the middle of the flat region.
DEFAULT_ALPHA = 30.0

# Below this many training rows a position keeps its base estimate untouched.
# ~2 seasons of a position's player-weeks; under that the ~34-column fit is not
# worth trusting over the rolling average it would be replacing.
MIN_TRAINING_ROWS = 400


class BlendModel:
    """Per-position ridge over FEATURES, predicting the week's actual points.

    Per-position for the same reason week1.py is: `carries` is a workload
    signal for a running back and a mobility signal for a quarterback, and the
    positions differ threefold in scale.

    A fitted model is applied only where it has training support; everywhere
    else `predict_one` returns the base estimate unchanged, so wiring this in
    can never make a position worse than not wiring it in.
    """

    def __init__(self, alpha: float = DEFAULT_ALPHA, features: Iterable[str] = FEATURES):
        self.alpha = alpha
        self.features = tuple(features)
        self._fits: dict[str, dict[str, Any]] = {}

    def _row(self, row: dict[str, Any], medians: dict[str, Optional[float]]) -> list[float]:
        vector, flags = [], []
        for feature in self.features:
            value = num(row.get(feature))
            flags.append(0.0 if value is not None else 1.0)
            if value is None:
                fallback = medians.get(feature)
                value = 0.0 if fallback is None else fallback
            vector.append(value)
        return vector + flags

    def fit(self, rows: list[dict[str, Any]], min_rows: int = MIN_TRAINING_ROWS) -> "BlendModel":
        """rows: dicts with "position", "actual_points", and any of FEATURES."""
        by_pos: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            position = row.get("position")
            if position is None or num(row.get("actual_points")) is None:
                continue
            by_pos.setdefault(position, []).append(row)

        for position, pos_rows in by_pos.items():
            if len(pos_rows) < min_rows:
                continue
            medians = {
                f: median([num(r.get(f)) for r in pos_rows if num(r.get(f)) is not None])
                for f in self.features
            }
            design = [self._row(r, medians) for r in pos_rows]
            targets = [float(num(r["actual_points"])) for r in pos_rows]
            mean, std = column_stats(design)
            beta = ridge([standardize(v, mean, std) for v in design], targets, self.alpha)
            self._fits[position] = {
                "beta": beta,
                "mean": mean,
                "std": std,
                "medians": medians,
                "ceiling": max(targets),
            }
        return self

    def predict_one(self, row: dict[str, Any]) -> float:
        """Refined points for one player-week.

        Falls back to the row's own `shrunk_avg` (then `rolling_avg`, then 0)
        for any position without a fit, so an unfitted position is a no-op
        rather than a zero.
        """
        position = row.get("position")
        fit = self._fits.get(position)
        base = num(row.get("shrunk_avg"))
        if base is None:
            base = num(row.get("rolling_avg")) or 0.0
        if fit is None:
            return max(base, 0.0)

        z = standardize(self._row(row, fit["medians"]), fit["mean"], fit["std"], clip=DEFAULT_Z_CLIP)
        # Same two guards week1.py needed: a linear model over unbounded inputs
        # will happily extrapolate past anything it has ever seen.
        return min(max(predict_linear(fit["beta"], z), 0.0), fit["ceiling"])

    def predict(self, rows: Iterable[dict[str, Any]]) -> dict[str, float]:
        return {r["player_id"]: self.predict_one(r) for r in rows}

    @property
    def fitted_positions(self) -> tuple[str, ...]:
        return tuple(sorted(self._fits))


def rolling_volume(
    game_log: list[dict[str, Any]],
    season: int,
    week: int,
    window: int,
    decay: float = 1.0,
    columns: Iterable[str] = VOLUME_COLUMNS,
) -> dict[str, float]:
    """Decayed trailing averages of the volume columns, under the same as-of
    rule projections.games_before applies to points: strictly-prior games,
    same season only.

    Deliberately re-implemented here against the same window/decay the caller
    used for points rather than reusing usage.py, whose compute_usage_multiplier
    answers a different question (recent share vs trailing share, as a
    multiplier). These are levels, and the fit wants them on the same footing as
    the points average sitting next to them.
    """
    eligible = [g for g in game_log if g.get("season") == season and g.get("week", 0) < week]
    eligible.sort(key=lambda g: (g.get("season", 0), g.get("week", 0)))
    recent = eligible[-window:] if window > 0 else eligible
    if not recent:
        return {}

    n = len(recent)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    total_weight = sum(weights)
    out: dict[str, float] = {}
    for column in columns:
        acc, seen = 0.0, 0.0
        for w, game in zip(weights, recent):
            value = num(game.get(column))
            if value is None:
                continue
            acc += w * value
            seen += w
        # A column absent for every game in the window (a QB has no target
        # share) stays absent rather than becoming 0, so the missing-indicator
        # column carries it instead of the fit reading a real zero.
        if seen > 0:
            out[column] = acc / seen
    return out


def build_feature_row(
    player_id: str,
    position: str,
    projection: dict[str, Any],
    volume: dict[str, float],
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble one BlendModel row from a projections.project_player output,
    a rolling_volume() result, and this week's game context."""
    from context import context_features

    row: dict[str, Any] = {
        "player_id": player_id,
        "position": position,
        "rolling_avg": projection.get("rolling_avg"),
        "shrunk_avg": projection.get("shrunk_avg"),
        "games_used": projection.get("games_used"),
    }
    row.update(volume)
    row.update(context_features(context))
    return row
