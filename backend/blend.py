"""In-season blend: per-position ridge over rolling volume and game context. See ARCHITECTURE.md §4."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ridge import DEFAULT_Z_CLIP, column_stats, median, num, predict_linear, ridge, standardize

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
    "offense_pct",
)

CONTEXT_COLUMNS: tuple[str, ...] = (
    "implied_team_total",
    "total_line",
    "spread_line",
    "is_home",
)

BASE_COLUMNS: tuple[str, ...] = ("rolling_avg", "shrunk_avg", "games_used")

FEATURES: tuple[str, ...] = BASE_COLUMNS + VOLUME_COLUMNS + CONTEXT_COLUMNS

DEFAULT_ALPHA = 30.0

TRAINED_COVERAGE = 0.9
PREDICTED_COVERAGE = 0.5
COVERAGE_MIN_ROWS = 20


class BlendFeatureMismatch(ValueError):
    """A feature the blend was trained with is missing at prediction time."""


def _in_season(row: dict[str, Any]) -> bool:
    return (num(row.get("games_used")) or 0) > 0


def _coverage(rows: list[dict[str, Any]], features: Iterable[str]) -> dict[str, float]:
    return {f: sum(num(r.get(f)) is not None for r in rows) / len(rows) for f in features}


MIN_TRAINING_ROWS = 400


class BlendModel:
    """Per-position ridge over FEATURES, predicting the week's actual points."""

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
        """Fit one ridge per position on rows with position, actual_points and FEATURES."""
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
            in_season = [r for r in pos_rows if _in_season(r)]
            self._fits[position] = {
                "beta": beta,
                "mean": mean,
                "std": std,
                "medians": medians,
                "ceiling": max(targets),
                "coverage": _coverage(in_season, self.features) if in_season else {},
            }
        return self

    def check_coverage(self, rows: Iterable[dict[str, Any]]) -> None:
        """Raise BlendFeatureMismatch if a feature the fit nearly always had is missing."""
        by_pos: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if _in_season(row) and row.get("position") in self._fits:
                by_pos.setdefault(row["position"], []).append(row)

        problems = []
        for position, pos_rows in sorted(by_pos.items()):
            if len(pos_rows) < COVERAGE_MIN_ROWS:
                continue
            trained = self._fits[position].get("coverage") or {}
            predicted = _coverage(pos_rows, self.features)
            for feature in self.features:
                if trained.get(feature, 0.0) >= TRAINED_COVERAGE and predicted[feature] < PREDICTED_COVERAGE:
                    problems.append(
                        f"{position}.{feature}: {trained[feature]:.0%} of training rows, "
                        f"{predicted[feature]:.0%} of {len(pos_rows)} prediction rows"
                    )
        if problems:
            raise BlendFeatureMismatch(
                "blend features present in training are missing at prediction time -- "
                "projections would collapse toward the positional mean. Fix the loader, "
                "or run with --skip-blend. " + "; ".join(problems)
            )

    def predict_one(self, row: dict[str, Any]) -> float:
        """Refined points for one player-week."""
        position = row.get("position")
        fit = self._fits.get(position)
        base = num(row.get("shrunk_avg"))
        if base is None:
            base = num(row.get("rolling_avg")) or 0.0
        if fit is None:
            return max(base, 0.0)

        z = standardize(self._row(row, fit["medians"]), fit["mean"], fit["std"], clip=DEFAULT_Z_CLIP)
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
    """Decayed trailing averages of the volume columns (strictly prior, same season)."""
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
    """One BlendModel row from a projection, rolling volume and game context."""
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
