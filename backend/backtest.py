"""Historical backtest / model-tuning harness. See ARCHITECTURE.md §11."""

from __future__ import annotations

import json
import math
from typing import Any, Optional

import baseline
import usage
from matchup import compute_opponent_multiplier
from projections import (
    DEFAULT_DECAY,
    DEFAULT_SHRINKAGE_MODE,
    DEFAULT_SHRINKAGE_STRENGTH,
    DEFAULT_WINDOW,
    project_player,
)
from scoring import compute_league_points, nflreadpy_row_to_stat_line


def evaluate_player_week(
    game_log: list[dict[str, Any]],
    scoring_config: dict[str, Any],
    season: int,
    week: int,
    window: int = DEFAULT_WINDOW,
    opponent_multiplier: float = 1.0,
    decay: float = DEFAULT_DECAY,
    positional_baseline: Optional[float] = None,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
    usage_multiplier: float = 1.0,
    shrinkage_mode: str = DEFAULT_SHRINKAGE_MODE,
    shrinkage_k: Optional[float] = None,
    position: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Grade one player's projection against one already-known week."""
    actual_entry = next((g for g in game_log if g["season"] == season and g["week"] == week), None)
    if actual_entry is None:
        return None

    projection = project_player(
        game_log, scoring_config, season, week, window=window, opponent_multiplier=opponent_multiplier, decay=decay,
        positional_baseline=positional_baseline, shrinkage_strength=shrinkage_strength,
        usage_multiplier=usage_multiplier, shrinkage_mode=shrinkage_mode,
        shrinkage_k=shrinkage_k,
    )
    actual_points = float(compute_league_points(actual_entry, scoring_config))
    projected_points = projection["projected_points"]

    return {
        "season": season,
        "week": week,
        "projected": projected_points,
        "actual": actual_points,
        "error": round(actual_points - projected_points, 2),
        "abs_error": round(abs(actual_points - projected_points), 2),
        "sq_error": round((actual_points - projected_points) ** 2, 4),
        "confidence": projection["confidence"],
        "games_used": projection["games_used"],
        "position": position or next(
            (g.get("position") for g in game_log if g.get("position")), None
        ),
    }


def backtest_player(
    game_log: list[dict[str, Any]],
    scoring_config: dict[str, Any],
    season: int,
    weeks: list[int],
    window: int = DEFAULT_WINDOW,
    use_matchup: bool = False,
    schedule_games: Optional[list[dict[str, Any]]] = None,
    opponent_by_week: Optional[dict[int, str]] = None,
    decay: float = DEFAULT_DECAY,
    use_shrinkage: bool = False,
    baseline_by_week: Optional[dict[int, float]] = None,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
    use_usage: bool = False,
    usage_alpha: float = usage.DEFAULT_USAGE_ALPHA,
    usage_metric: str = usage.DEFAULT_USAGE_METRIC,
) -> list[dict[str, Any]]:
    """Run evaluate_player_week across a list of weeks for one player."""
    if use_matchup and not schedule_games:
        raise ValueError("use_matchup=True requires schedule_games")
    if use_shrinkage and baseline_by_week is None:
        raise ValueError("use_shrinkage=True requires baseline_by_week")

    results = []
    for week in weeks:
        multiplier = 1.0
        if use_matchup:
            opponent_team = (opponent_by_week or {}).get(week)
            if opponent_team is None:
                continue
            multiplier = compute_opponent_multiplier(
                schedule_games, opponent_team, season, week
            )["multiplier"]
        positional_baseline = (baseline_by_week or {}).get(week) if use_shrinkage else None
        usage_multiplier = 1.0
        if use_usage:
            usage_multiplier = usage.compute_usage_multiplier(
                game_log, season, week, metric=usage_metric, alpha=usage_alpha,
            )["multiplier"]
        row = evaluate_player_week(
            game_log, scoring_config, season, week, window=window, opponent_multiplier=multiplier, decay=decay,
            positional_baseline=positional_baseline, shrinkage_strength=shrinkage_strength,
            usage_multiplier=usage_multiplier,
        )
        if row is not None:
            results.append(row)
    return results


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _pearson_correlation(xs: list[float], ys: list[float]) -> Optional[float]:
    """Plain-stdlib Pearson correlation; None for a degenerate series."""
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = _mean(xs), _mean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return round(covariance / (var_x * var_y) ** 0.5, 3)


def _std(values: list[float]) -> float:
    """Sample standard deviation (ddof=1)."""
    n = len(values)
    mean = _mean(values)
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def _standard_normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def paired_significance_test(a: list[float], b: list[float], alpha: float = 0.05) -> dict[str, Any]:
    """Paired z-test on per-pair differences between two variants."""
    if len(a) != len(b):
        raise ValueError("paired_significance_test requires equal-length paired samples")
    n = len(a)
    if n < 2:
        return {"n": n, "mean_diff": None, "p_value": None, "significant": None}

    diffs = [x - y for x, y in zip(a, b)]
    mean_diff = _mean(diffs)
    std_diff = _std(diffs)

    if std_diff == 0:
        p_value = 0.0 if mean_diff != 0 else 1.0
    else:
        z = mean_diff / (std_diff / math.sqrt(n))
        p_value = round(2 * (1 - _standard_normal_cdf(abs(z))), 4)

    return {
        "n": n,
        "mean_diff": round(mean_diff, 4),
        "p_value": p_value,
        "significant": p_value < alpha,
    }


def pairwise_accuracy(results: list[dict[str, Any]]) -> Optional[float]:
    """Share of same-position, same-week pairs ordered correctly (0.50 is a coin flip)."""
    buckets: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for row in results:
        buckets.setdefault((row.get("season"), row.get("week"), row.get("position")), []).append(row)

    hits = total = 0
    for rows in buckets.values():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                dp = a["projected"] - b["projected"]
                da = a["actual"] - b["actual"]
                if dp == 0 or da == 0:
                    continue
                total += 1
                if (dp > 0) == (da > 0):
                    hits += 1
    return None if total == 0 else hits / total


def _spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    """Rank correlation, average ranks for ties."""
    if len(xs) < 3:
        return None
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out
    return _pearson_correlation(ranks(xs), ranks(ys))


def aggregate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline metrics for pooled evaluate_player_week rows; RMSE is primary."""
    if not results:
        return {
            "n": 0, "rmse": None, "mae": None, "bias": None, "correlation": None,
            "spearman": None, "pairwise_accuracy": None, "by_confidence": {},
        }

    abs_errors = [r["abs_error"] for r in results]
    sq_errors = [r.get("sq_error", (r["actual"] - r["projected"]) ** 2) for r in results]
    errors = [r["error"] for r in results]
    projected = [r["projected"] for r in results]
    actual = [r["actual"] for r in results]

    by_confidence = {}
    for tier in sorted({r["confidence"] for r in results}):
        tier_rows = [r for r in results if r["confidence"] == tier]
        tier_sq = [t.get("sq_error", (t["actual"] - t["projected"]) ** 2) for t in tier_rows]
        by_confidence[tier] = {
            "n": len(tier_rows),
            "rmse": round(math.sqrt(_mean(tier_sq)), 3),
            "mae": round(_mean([r["abs_error"] for r in tier_rows]), 3),
            "bias": round(_mean([r["error"] for r in tier_rows]), 3),
        }

    ordered_errors = sorted(errors)
    mid = len(ordered_errors) // 2
    median_bias = (
        ordered_errors[mid]
        if len(ordered_errors) % 2
        else (ordered_errors[mid - 1] + ordered_errors[mid]) / 2
    )

    return {
        "n": len(results),
        "rmse": round(math.sqrt(_mean(sq_errors)), 3),
        "mae": round(_mean(abs_errors), 3),
        "bias": round(_mean(errors), 3),
        "median_bias": round(median_bias, 3),
        "correlation": _pearson_correlation(projected, actual),
        "spearman": _spearman(projected, actual),
        "pairwise_accuracy": pairwise_accuracy(results),
        "by_confidence": by_confidence,
    }


def compare_variants(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    weeks: list[int],
    variants: list[dict[str, Any]],
    schedule_games: Optional[list[dict[str, Any]]] = None,
    opponent_by_week_by_player: Optional[dict[str, dict[int, str]]] = None,
    baseline_by_week_by_player: Optional[dict[str, dict[int, float]]] = None,
) -> dict[str, dict[str, Any]]:
    """Run the full pool through each variant config; aggregated metrics per variant."""
    out = {}
    for variant in variants:
        pooled: list[dict[str, Any]] = []
        for player_id, game_log in game_logs_by_player.items():
            opponent_by_week = (opponent_by_week_by_player or {}).get(player_id)
            baseline_by_week = (baseline_by_week_by_player or {}).get(player_id)
            pooled.extend(
                backtest_player(
                    game_log,
                    scoring_config,
                    season,
                    weeks,
                    window=variant["window"],
                    use_matchup=variant.get("use_matchup", False),
                    schedule_games=schedule_games,
                    opponent_by_week=opponent_by_week,
                    decay=variant.get("decay", DEFAULT_DECAY),
                    use_shrinkage=variant.get("use_shrinkage", False),
                    baseline_by_week=baseline_by_week,
                    shrinkage_strength=variant.get("shrinkage_strength", DEFAULT_SHRINKAGE_STRENGTH),
                    use_usage=variant.get("use_usage", False),
                    usage_alpha=variant.get("usage_alpha", usage.DEFAULT_USAGE_ALPHA),
                    usage_metric=variant.get("usage_metric", usage.DEFAULT_USAGE_METRIC),
                )
            )
        out[variant["label"]] = aggregate_metrics(pooled)
    return out


def paired_variant_metric(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    weeks: list[int],
    variant_a: dict[str, Any],
    variant_b: dict[str, Any],
    metric: str = "abs_error",
    schedule_games: Optional[list[dict[str, Any]]] = None,
    opponent_by_week_by_player: Optional[dict[str, dict[int, str]]] = None,
    baseline_by_week_by_player: Optional[dict[str, dict[int, float]]] = None,
    confidence_filter: Optional[str] = None,
) -> tuple[list[float], list[float]]:
    """Matched per-row metric values for two variants, on rows both graded."""
    if confidence_filter is not None and confidence_filter not in ("no_data", "low", "full"):
        raise ValueError(f"confidence_filter must be one of 'no_data', 'low', 'full', got {confidence_filter!r}")

    values_a: list[float] = []
    values_b: list[float] = []
    for player_id, game_log in game_logs_by_player.items():
        opponent_by_week = (opponent_by_week_by_player or {}).get(player_id)
        baseline_by_week = (baseline_by_week_by_player or {}).get(player_id)
        rows_a = {
            r["week"]: r
            for r in backtest_player(
                game_log, scoring_config, season, weeks,
                window=variant_a["window"], use_matchup=variant_a.get("use_matchup", False),
                schedule_games=schedule_games, opponent_by_week=opponent_by_week,
                decay=variant_a.get("decay", DEFAULT_DECAY),
                use_shrinkage=variant_a.get("use_shrinkage", False), baseline_by_week=baseline_by_week,
                shrinkage_strength=variant_a.get("shrinkage_strength", DEFAULT_SHRINKAGE_STRENGTH),
                use_usage=variant_a.get("use_usage", False),
                usage_alpha=variant_a.get("usage_alpha", usage.DEFAULT_USAGE_ALPHA),
                usage_metric=variant_a.get("usage_metric", usage.DEFAULT_USAGE_METRIC),
            )
        }
        rows_b = {
            r["week"]: r
            for r in backtest_player(
                game_log, scoring_config, season, weeks,
                window=variant_b["window"], use_matchup=variant_b.get("use_matchup", False),
                schedule_games=schedule_games, opponent_by_week=opponent_by_week,
                decay=variant_b.get("decay", DEFAULT_DECAY),
                use_shrinkage=variant_b.get("use_shrinkage", False), baseline_by_week=baseline_by_week,
                shrinkage_strength=variant_b.get("shrinkage_strength", DEFAULT_SHRINKAGE_STRENGTH),
                use_usage=variant_b.get("use_usage", False),
                usage_alpha=variant_b.get("usage_alpha", usage.DEFAULT_USAGE_ALPHA),
                usage_metric=variant_b.get("usage_metric", usage.DEFAULT_USAGE_METRIC),
            )
        }
        for week in sorted(set(rows_a) & set(rows_b)):
            row_a, row_b = rows_a[week], rows_b[week]
            if confidence_filter is not None and row_a["confidence"] != confidence_filter:
                continue
            values_a.append(row_a[metric])
            values_b.append(row_b[metric])
    return values_a, values_b


def print_report(label: str, metrics: dict[str, Any]) -> None:
    """Human-readable console summary."""
    print(f"\n=== {label} ===")
    if metrics["n"] == 0:
        print("  no graded weeks")
        return
    print(
        f"  n={metrics['n']}  RMSE={metrics['rmse']}  MAE={metrics['mae']}  "
        f"bias={metrics['bias']:+}  median_bias={metrics.get('median_bias'):+}"
    )
    pair = metrics.get("pairwise_accuracy")
    print(
        f"  ranking: pairwise start/sit={'n/a' if pair is None else format(pair, '.4f')}"
        f"  spearman={metrics.get('spearman')}  pearson={metrics['correlation']}"
    )
    for tier, tier_metrics in metrics["by_confidence"].items():
        print(
            f"    [{tier}] n={tier_metrics['n']}  RMSE={tier_metrics.get('rmse')}  "
            f"MAE={tier_metrics['mae']}  bias={tier_metrics['bias']:+}"
        )


SEASON = 2025
POSITIONS = ("QB", "RB", "WR", "TE")
TUNING_WEEKS = list(range(5, 15))
HOLDOUT_WEEKS = list(range(15, 19))
WINDOW_SIZES_TO_SWEEP = [3, 4, 5, 6]
DECAY_VALUES_TO_SWEEP = [1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6]
SHRINKAGE_STRENGTHS_TO_SWEEP = [0.0, 0.25, 0.5, 0.75, 1.0]
USAGE_ALPHAS_TO_SWEEP = [0.0, 0.25, 0.5, 0.75, 1.0]


def load_full_pool_game_logs(season: int, positions: tuple[str, ...] = POSITIONS) -> dict[str, list[dict[str, Any]]]:
    """Game logs for every player at `positions` with a stat line in `season`."""
    import nflreadpy as nfl

    stats = nfl.load_player_stats(seasons=[season])
    df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
    df = df[df["position"].isin(positions)]

    game_logs: dict[str, list[dict[str, Any]]] = {}
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        stat_line = nflreadpy_row_to_stat_line(row_dict)
        stat_line["season"] = season
        stat_line["week"] = int(row["week"])
        stat_line["opponent_team"] = row.get("opponent_team")
        stat_line["position"] = row.get("position")
        for col in usage.USAGE_COLUMNS:
            stat_line[col] = row.get(col)
        game_logs.setdefault(row["player_id"], []).append(stat_line)
    return game_logs


def load_schedule_games(season: int) -> list[dict[str, Any]]:
    import nflreadpy as nfl

    schedules = nfl.load_schedules(seasons=[season, season - 1])
    df = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
    cols = ["season", "week", "home_team", "away_team", "home_score", "away_score"]
    return df[cols].to_dict("records")


def main():
    with open("leagues/league-1/scoring-config.json") as f:
        scoring_config = json.load(f)

    game_logs = load_full_pool_game_logs(SEASON, POSITIONS)
    print(f"Loaded full active pool: {len(game_logs)} players at {POSITIONS} for {SEASON}.")

    print("#" * 60)
    print("WINDOW SIZE SWEEP -- TUNING WEEKS")
    print("#" * 60)
    window_variants = [{"label": f"window={w}", "window": w, "use_matchup": False} for w in WINDOW_SIZES_TO_SWEEP]
    tuning_window_results = compare_variants(game_logs, scoring_config, SEASON, TUNING_WEEKS, window_variants)
    for label, metrics in tuning_window_results.items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("WINDOW SIZE SWEEP -- HOLDOUT WEEKS (confirm before committing)")
    print("#" * 60)
    for label, metrics in compare_variants(game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, window_variants).items():
        print_report(label, metrics)

    window_candidates = [w for w in WINDOW_SIZES_TO_SWEEP if w != DEFAULT_WINDOW]
    best_window = min(window_candidates, key=lambda w: tuning_window_results[f"window={w}"]["mae"])
    print(f"\nBest tuning-weeks MAE among window!={DEFAULT_WINDOW} candidates: window={best_window}")

    print("\n" + "#" * 60)
    print(f"WINDOW SIZE SIGNIFICANCE TEST (window={best_window} vs window={DEFAULT_WINDOW}, tuning weeks)")
    print("#" * 60)
    default_window_variant = {"window": DEFAULT_WINDOW}
    best_window_variant = {"window": best_window}
    tuning_default_abs_error, tuning_best_abs_error = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, default_window_variant, best_window_variant, metric="abs_error",
    )
    window_tuning_test = paired_significance_test(tuning_best_abs_error, tuning_default_abs_error)
    print(
        f"  abs_error (MAE):  n={window_tuning_test['n']}  mean_diff({best_window}-{DEFAULT_WINDOW})={window_tuning_test['mean_diff']:+}"
        f"  p={window_tuning_test['p_value']}  significant={window_tuning_test['significant']}"
    )

    print("\n" + "#" * 60)
    print(f"WINDOW SIZE SIGNIFICANCE TEST (window={best_window} vs window={DEFAULT_WINDOW}, holdout weeks)")
    print("#" * 60)
    holdout_default_abs_error, holdout_best_abs_error = paired_variant_metric(
        game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, default_window_variant, best_window_variant, metric="abs_error",
    )
    window_holdout_test = paired_significance_test(holdout_best_abs_error, holdout_default_abs_error)
    print(
        f"  abs_error (MAE):  n={window_holdout_test['n']}  mean_diff({best_window}-{DEFAULT_WINDOW})={window_holdout_test['mean_diff']:+}"
        f"  p={window_holdout_test['p_value']}  significant={window_holdout_test['significant']}"
    )

    runner_up_candidates = [w for w in window_candidates if w != best_window]
    if runner_up_candidates:
        runner_up_window = min(runner_up_candidates, key=lambda w: tuning_window_results[f"window={w}"]["mae"])
        print("\n" + "#" * 60)
        print(f"WINDOW SIZE SIGNIFICANCE TEST (window={best_window} vs runner-up window={runner_up_window}, tuning weeks)")
        print("#" * 60)
        runner_up_window_variant = {"window": runner_up_window}
        tuning_runner_up_abs_error, tuning_best_abs_error_2 = paired_variant_metric(
            game_logs, scoring_config, SEASON, TUNING_WEEKS, runner_up_window_variant, best_window_variant, metric="abs_error",
        )
        window_runner_up_test = paired_significance_test(tuning_best_abs_error_2, tuning_runner_up_abs_error)
        print(
            f"  abs_error (MAE):  n={window_runner_up_test['n']}  mean_diff({best_window}-{runner_up_window})={window_runner_up_test['mean_diff']:+}"
            f"  p={window_runner_up_test['p_value']}  significant={window_runner_up_test['significant']}"
        )

    print("\n" + "#" * 60)
    print("MATCHUP MULTIPLIER ABLATION -- TUNING WEEKS (window=%d)" % DEFAULT_WINDOW)
    print("#" * 60)
    schedule_games = load_schedule_games(SEASON)
    opponent_by_week_by_player = {
        pid: {g["week"]: g["opponent_team"] for g in log if g.get("opponent_team")}
        for pid, log in game_logs.items()
    }
    ablation_variants = [
        {"label": "matchup=off", "window": DEFAULT_WINDOW, "use_matchup": False},
        {"label": "matchup=on", "window": DEFAULT_WINDOW, "use_matchup": True},
    ]
    ablation_results = compare_variants(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, ablation_variants,
        schedule_games=schedule_games, opponent_by_week_by_player=opponent_by_week_by_player,
    )
    for label, metrics in ablation_results.items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("MATCHUP MULTIPLIER SIGNIFICANCE TEST (window=%d, tuning weeks)" % DEFAULT_WINDOW)
    print("#" * 60)
    off_variant = {"window": DEFAULT_WINDOW, "use_matchup": False}
    on_variant = {"window": DEFAULT_WINDOW, "use_matchup": True}
    off_abs_error, on_abs_error = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, off_variant, on_variant,
        metric="abs_error", schedule_games=schedule_games,
        opponent_by_week_by_player=opponent_by_week_by_player,
    )
    mae_test = paired_significance_test(on_abs_error, off_abs_error)
    print(
        f"  abs_error (MAE):  n={mae_test['n']}  mean_diff(on-off)={mae_test['mean_diff']:+}"
        f"  p={mae_test['p_value']}  significant={mae_test['significant']}"
    )
    off_error, on_error = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, off_variant, on_variant,
        metric="error", schedule_games=schedule_games,
        opponent_by_week_by_player=opponent_by_week_by_player,
    )
    bias_test = paired_significance_test(on_error, off_error)
    print(
        f"  error (bias):      n={bias_test['n']}  mean_diff(on-off)={bias_test['mean_diff']:+}"
        f"  p={bias_test['p_value']}  significant={bias_test['significant']}"
    )

    print("\n" + "#" * 60)
    print("DECAY SWEEP -- TUNING WEEKS (window=%d)" % DEFAULT_WINDOW)
    print("#" * 60)
    decay_variants = [
        {"label": f"decay={d}", "window": DEFAULT_WINDOW, "use_matchup": False, "decay": d}
        for d in DECAY_VALUES_TO_SWEEP
    ]
    tuning_decay_results = compare_variants(game_logs, scoring_config, SEASON, TUNING_WEEKS, decay_variants)
    for label, metrics in tuning_decay_results.items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("DECAY SWEEP -- HOLDOUT WEEKS (confirm before committing)")
    print("#" * 60)
    holdout_decay_results = compare_variants(game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, decay_variants)
    for label, metrics in holdout_decay_results.items():
        print_report(label, metrics)

    candidates = [v for v in decay_variants if v["decay"] != 1.0]
    best_decay_label = min(candidates, key=lambda v: tuning_decay_results[v["label"]]["mae"])["decay"]
    print(f"\nBest tuning-weeks MAE among decay<1.0 candidates: decay={best_decay_label}")

    print("\n" + "#" * 60)
    print(f"DECAY SIGNIFICANCE TEST (decay={best_decay_label} vs decay=1.0, window={DEFAULT_WINDOW}, tuning weeks)")
    print("#" * 60)
    baseline_variant = {"window": DEFAULT_WINDOW, "decay": 1.0}
    candidate_variant = {"window": DEFAULT_WINDOW, "decay": best_decay_label}
    baseline_abs_error, candidate_abs_error = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, baseline_variant, candidate_variant, metric="abs_error",
    )
    decay_mae_test = paired_significance_test(candidate_abs_error, baseline_abs_error)
    print(
        f"  abs_error (MAE):  n={decay_mae_test['n']}  mean_diff(decay-baseline)={decay_mae_test['mean_diff']:+}"
        f"  p={decay_mae_test['p_value']}  significant={decay_mae_test['significant']}"
    )
    baseline_error, candidate_error = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, baseline_variant, candidate_variant, metric="error",
    )
    decay_bias_test = paired_significance_test(candidate_error, baseline_error)
    print(
        f"  error (bias):      n={decay_bias_test['n']}  mean_diff(decay-baseline)={decay_bias_test['mean_diff']:+}"
        f"  p={decay_bias_test['p_value']}  significant={decay_bias_test['significant']}"
    )

    print("\n" + "#" * 60)
    print("POSITION-SPECIFIC WINDOW SWEEP -- TUNING WEEKS")
    print("#" * 60)
    positions_by_player = {
        pid: next((g["position"] for g in log if g.get("position")), None)
        for pid, log in game_logs.items()
    }
    positions = sorted({pos for pos in positions_by_player.values() if pos})
    for position in positions:
        position_game_logs = {pid: log for pid, log in game_logs.items() if positions_by_player[pid] == position}
        print(f"\n-- {position} (n_players={len(position_game_logs)}) --")
        position_results = compare_variants(position_game_logs, scoring_config, SEASON, TUNING_WEEKS, window_variants)
        for label, metrics in position_results.items():
            print_report(label, metrics)

        position_best_window = min(WINDOW_SIZES_TO_SWEEP, key=lambda w: position_results[f"window={w}"]["mae"])
        if position_best_window == DEFAULT_WINDOW:
            print(f"   (best window for {position} is already DEFAULT_WINDOW={DEFAULT_WINDOW} -- nothing to test)")
            continue
        best_values, default_values = paired_variant_metric(
            position_game_logs, scoring_config, SEASON, TUNING_WEEKS,
            variant_a={"window": position_best_window}, variant_b={"window": DEFAULT_WINDOW}, metric="abs_error",
        )
        position_sig_test = paired_significance_test(best_values, default_values)
        print(
            f"   significance test: window={position_best_window} vs window={DEFAULT_WINDOW} (default) -- "
            f"n={position_sig_test['n']}  mean_diff={position_sig_test['mean_diff']}"
            f"  p={position_sig_test['p_value']}  significant={position_sig_test['significant']}"
        )

    print("\n" + "#" * 60)
    print("POSITIONAL BASELINES -- SANITY CHECK")
    print("#" * 60)
    all_weeks = sorted(set(TUNING_WEEKS) | set(HOLDOUT_WEEKS))
    debut_baselines_by_week = baseline.positional_baselines_by_week(
        game_logs, scoring_config, SEASON, all_weeks, population="debut",
        min_week=baseline.DEFAULT_DEBUT_MIN_WEEK,
    )
    thin_baselines_by_week = baseline.positional_baselines_by_week(
        game_logs, scoring_config, SEASON, all_weeks, population="thin",
    )
    baseline_by_week_by_player = baseline.baselines_by_player_week_for_shrinkage(
        game_logs, SEASON, DEFAULT_WINDOW, debut_baselines_by_week, thin_baselines_by_week,
    )
    for week in (TUNING_WEEKS[0], HOLDOUT_WEEKS[0]):
        print(f"  week {week} debut: {debut_baselines_by_week[week]}")
        print(f"  week {week} thin:  {thin_baselines_by_week[week]}")

    print("\n" + "#" * 60)
    print("BASELINE ABLATION (fallback-only, strength=0.0) -- TUNING WEEKS")
    print("#" * 60)
    baseline_ablation_variants = [
        {"label": "baseline=off", "window": DEFAULT_WINDOW, "use_shrinkage": False},
        {"label": "baseline=on", "window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": 0.0},
    ]
    for label, metrics in compare_variants(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, baseline_ablation_variants,
        baseline_by_week_by_player=baseline_by_week_by_player,
    ).items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("BASELINE ABLATION (fallback-only, strength=0.0) -- HOLDOUT WEEKS")
    print("#" * 60)
    for label, metrics in compare_variants(
        game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, baseline_ablation_variants,
        baseline_by_week_by_player=baseline_by_week_by_player,
    ).items():
        print_report(label, metrics)

    off_variant = {"window": DEFAULT_WINDOW, "use_shrinkage": False}
    fallback_variant = {"window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": 0.0}
    for weeks_label, weeks in (("tuning", TUNING_WEEKS), ("holdout", HOLDOUT_WEEKS)):
        print("\n" + "#" * 60)
        print(f"BASELINE SIGNIFICANCE TEST -- no_data tier bias ({weeks_label} weeks)")
        print("#" * 60)
        off_err, on_err = paired_variant_metric(
            game_logs, scoring_config, SEASON, weeks, off_variant, fallback_variant, metric="error",
            baseline_by_week_by_player=baseline_by_week_by_player, confidence_filter="no_data",
        )
        no_data_bias_test = paired_significance_test(on_err, off_err)
        print(
            f"  error (bias):  n={no_data_bias_test['n']}  mean_diff(on-off)={no_data_bias_test['mean_diff']:+}"
            f"  p={no_data_bias_test['p_value']}  significant={no_data_bias_test['significant']}"
        )

    print("\n" + "#" * 60)
    print("BASELINE SIGNIFICANCE TEST -- pooled MAE guardrail (tuning weeks)")
    print("#" * 60)
    off_abs, on_abs = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, off_variant, fallback_variant, metric="abs_error",
        baseline_by_week_by_player=baseline_by_week_by_player,
    )
    pooled_mae_guardrail_test = paired_significance_test(on_abs, off_abs)
    print(
        f"  abs_error (MAE):  n={pooled_mae_guardrail_test['n']}  mean_diff(on-off)={pooled_mae_guardrail_test['mean_diff']:+}"
        f"  p={pooled_mae_guardrail_test['p_value']}  significant={pooled_mae_guardrail_test['significant']}"
    )

    print("\n" + "#" * 60)
    print("SHRINKAGE STRENGTH SWEEP (low tier) -- TUNING WEEKS")
    print("#" * 60)
    strength_variants = [
        {"label": f"strength={s}", "window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": s}
        for s in SHRINKAGE_STRENGTHS_TO_SWEEP
    ]
    tuning_strength_results = compare_variants(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, strength_variants,
        baseline_by_week_by_player=baseline_by_week_by_player,
    )
    for label, metrics in tuning_strength_results.items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("SHRINKAGE STRENGTH SWEEP (low tier) -- HOLDOUT WEEKS")
    print("#" * 60)
    for label, metrics in compare_variants(
        game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, strength_variants,
        baseline_by_week_by_player=baseline_by_week_by_player,
    ).items():
        print_report(label, metrics)

    best_strength = min(
        SHRINKAGE_STRENGTHS_TO_SWEEP,
        key=lambda s: abs(tuning_strength_results[f"strength={s}"]["by_confidence"].get("low", {"bias": 0})["bias"]),
    )
    print(f"\nBest tuning-weeks |low-tier bias| among swept strengths: strength={best_strength}")

    best_strength_variant = {"window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": best_strength}
    for weeks_label, weeks in (("tuning", TUNING_WEEKS), ("holdout", HOLDOUT_WEEKS)):
        print("\n" + "#" * 60)
        print(f"SHRINKAGE SIGNIFICANCE TEST -- low tier bias, strength={best_strength} vs off ({weeks_label} weeks)")
        print("#" * 60)
        off_err, best_err = paired_variant_metric(
            game_logs, scoring_config, SEASON, weeks, off_variant, best_strength_variant, metric="error",
            baseline_by_week_by_player=baseline_by_week_by_player, confidence_filter="low",
        )
        low_bias_test = paired_significance_test(best_err, off_err)
        print(
            f"  error (bias):  n={low_bias_test['n']}  mean_diff(on-off)={low_bias_test['mean_diff']:+}"
            f"  p={low_bias_test['p_value']}  significant={low_bias_test['significant']}"
        )

    for weeks_label, weeks in (("tuning", TUNING_WEEKS), ("holdout", HOLDOUT_WEEKS)):
        print("\n" + "#" * 60)
        print(f"SHRINKAGE SIGNIFICANCE TEST -- pooled MAE guardrail, strength={best_strength} vs off ({weeks_label} weeks)")
        print("#" * 60)
        off_abs, best_abs = paired_variant_metric(
            game_logs, scoring_config, SEASON, weeks, off_variant, best_strength_variant, metric="abs_error",
            baseline_by_week_by_player=baseline_by_week_by_player,
        )
        strength_mae_guardrail_test = paired_significance_test(best_abs, off_abs)
        print(
            f"  abs_error (MAE):  n={strength_mae_guardrail_test['n']}  mean_diff(on-off)={strength_mae_guardrail_test['mean_diff']:+}"
            f"  p={strength_mae_guardrail_test['p_value']}  significant={strength_mae_guardrail_test['significant']}"
        )

    runner_up_candidates = [s for s in SHRINKAGE_STRENGTHS_TO_SWEEP if s != best_strength]
    if runner_up_candidates:
        runner_up_strength = min(
            runner_up_candidates,
            key=lambda s: abs(tuning_strength_results[f"strength={s}"]["by_confidence"].get("low", {"bias": 0})["bias"]),
        )
        runner_up_variant = {"window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": runner_up_strength}
        for weeks_label, weeks in (("tuning", TUNING_WEEKS), ("holdout", HOLDOUT_WEEKS)):
            print("\n" + "#" * 60)
            print(
                f"SHRINKAGE SIGNIFICANCE TEST -- low tier bias, strength={best_strength} vs "
                f"runner-up strength={runner_up_strength} ({weeks_label} weeks)"
            )
            print("#" * 60)
            runner_up_err, best_err_2 = paired_variant_metric(
                game_logs, scoring_config, SEASON, weeks, runner_up_variant, best_strength_variant, metric="error",
                baseline_by_week_by_player=baseline_by_week_by_player, confidence_filter="low",
            )
            runner_up_bias_test = paired_significance_test(best_err_2, runner_up_err)
            print(
                f"  error (bias):  n={runner_up_bias_test['n']}  mean_diff({best_strength}-{runner_up_strength})={runner_up_bias_test['mean_diff']:+}"
                f"  p={runner_up_bias_test['p_value']}  significant={runner_up_bias_test['significant']}"
            )

    print("\n" + "#" * 60)
    print("BASELINE POPULATION COMPARATOR (debut+thin vs flat \"all\") -- TUNING WEEKS, strength=1.0")
    print("#" * 60)
    all_pop_baselines_by_week = baseline.positional_baselines_by_week(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, population="all",
    )
    all_pop_baseline_by_week_by_player = baseline.baselines_by_player_week(game_logs, all_pop_baselines_by_week)
    population_variants = [
        {"label": "population=debut+thin", "window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": 1.0},
        {"label": "population=all", "window": DEFAULT_WINDOW, "use_shrinkage": True, "shrinkage_strength": 1.0},
    ]
    debut_thin_metrics = compare_variants(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, [population_variants[0]],
        baseline_by_week_by_player=baseline_by_week_by_player,
    )
    all_metrics = compare_variants(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, [population_variants[1]],
        baseline_by_week_by_player=all_pop_baseline_by_week_by_player,
    )
    print_report("population=debut+thin", debut_thin_metrics["population=debut+thin"])
    print_report("population=all", all_metrics["population=all"])

    print("\n" + "#" * 60)
    print("USAGE DATA COVERAGE -- SANITY CHECK")
    print("#" * 60)
    usage_coverage_total = 0
    usage_coverage_present = 0
    for game_log in game_logs.values():
        for week in all_weeks:
            actual_entry = next((g for g in game_log if g["season"] == SEASON and g["week"] == week), None)
            if actual_entry is None:
                continue
            usage_coverage_total += 1
            if usage.compute_usage_multiplier(game_log, SEASON, week)["source"] == "current_season":
                usage_coverage_present += 1
    coverage_pct = 100 * usage_coverage_present / usage_coverage_total if usage_coverage_total else 0.0
    print(f"  usage data present for {usage_coverage_present}/{usage_coverage_total} graded rows ({coverage_pct:.1f}%)")
    print("  (if this is low, stop and fix the loader passthrough before trusting any sweep below)")

    print("\n" + "#" * 60)
    print("USAGE ALPHA SWEEP -- TUNING WEEKS")
    print("#" * 60)
    usage_variants = [
        {"label": f"alpha={a}", "window": DEFAULT_WINDOW, "use_usage": True, "usage_alpha": a}
        for a in USAGE_ALPHAS_TO_SWEEP
    ]
    tuning_usage_results = compare_variants(game_logs, scoring_config, SEASON, TUNING_WEEKS, usage_variants)
    for label, metrics in tuning_usage_results.items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("USAGE ALPHA SWEEP -- HOLDOUT WEEKS")
    print("#" * 60)
    for label, metrics in compare_variants(game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, usage_variants).items():
        print_report(label, metrics)

    usage_candidates = [a for a in USAGE_ALPHAS_TO_SWEEP if a != 0.0]
    best_alpha = min(usage_candidates, key=lambda a: tuning_usage_results[f"alpha={a}"]["mae"])
    print(f"\nBest tuning-weeks MAE among alpha!=0.0 candidates: alpha={best_alpha}")

    off_usage_variant = {"window": DEFAULT_WINDOW, "use_usage": False}
    best_usage_variant = {"window": DEFAULT_WINDOW, "use_usage": True, "usage_alpha": best_alpha}
    for weeks_label, weeks in (("tuning", TUNING_WEEKS), ("holdout", HOLDOUT_WEEKS)):
        print("\n" + "#" * 60)
        print(f"USAGE SIGNIFICANCE TEST -- pooled MAE, alpha={best_alpha} vs off ({weeks_label} weeks)")
        print("#" * 60)
        off_abs, best_abs = paired_variant_metric(
            game_logs, scoring_config, SEASON, weeks, off_usage_variant, best_usage_variant, metric="abs_error",
        )
        usage_mae_test = paired_significance_test(best_abs, off_abs)
        print(
            f"  abs_error (MAE):  n={usage_mae_test['n']}  mean_diff(on-off)={usage_mae_test['mean_diff']:+}"
            f"  p={usage_mae_test['p_value']}  significant={usage_mae_test['significant']}"
        )

    print("\n" + "#" * 60)
    print(f"USAGE SIGNIFICANCE TEST -- low tier bias (secondary), alpha={best_alpha} vs off (tuning weeks)")
    print("#" * 60)
    off_err, best_err = paired_variant_metric(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, off_usage_variant, best_usage_variant, metric="error",
        confidence_filter="low",
    )
    usage_low_bias_test = paired_significance_test(best_err, off_err)
    print(
        f"  error (bias):  n={usage_low_bias_test['n']}  mean_diff(on-off)={usage_low_bias_test['mean_diff']:+}"
        f"  p={usage_low_bias_test['p_value']}  significant={usage_low_bias_test['significant']}"
    )

    print("\n" + "#" * 60)
    print(f"USAGE PER-POSITION MAE, alpha={best_alpha} vs off (tuning weeks) -- QB should show ~0 delta")
    print("#" * 60)
    for position in positions:
        position_game_logs = {pid: log for pid, log in game_logs.items() if positions_by_player[pid] == position}
        off_abs_pos, best_abs_pos = paired_variant_metric(
            position_game_logs, scoring_config, SEASON, TUNING_WEEKS, off_usage_variant, best_usage_variant,
            metric="abs_error",
        )
        position_usage_test = paired_significance_test(best_abs_pos, off_abs_pos)
        print(
            f"  {position}: n={position_usage_test['n']}  mean_diff(on-off)={position_usage_test['mean_diff']}"
            f"  p={position_usage_test['p_value']}  significant={position_usage_test['significant']}"
        )

    print("\n" + "#" * 60)
    print(f"USAGE METRIC COMPARATOR (wopr vs target_share) -- TUNING WEEKS, alpha={best_alpha}")
    print("#" * 60)
    metric_comparator_variants = [
        {"label": "metric=wopr", "window": DEFAULT_WINDOW, "use_usage": True, "usage_alpha": best_alpha, "usage_metric": "wopr"},
        {"label": "metric=target_share", "window": DEFAULT_WINDOW, "use_usage": True, "usage_alpha": best_alpha, "usage_metric": "target_share"},
    ]
    metric_results = compare_variants(game_logs, scoring_config, SEASON, TUNING_WEEKS, metric_comparator_variants)
    for label, metrics in metric_results.items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("SHRINKAGE + USAGE INTERACTION -- TUNING WEEKS")
    print("#" * 60)
    both_on_variant = {
        "label": "both=on", "window": DEFAULT_WINDOW,
        "use_shrinkage": True, "shrinkage_strength": DEFAULT_SHRINKAGE_STRENGTH,
        "use_usage": True, "usage_alpha": best_alpha,
    }
    interaction_variants = [
        {"label": "neither", "window": DEFAULT_WINDOW},
        {"label": "shrinkage_only", "window": DEFAULT_WINDOW,
         "use_shrinkage": True, "shrinkage_strength": DEFAULT_SHRINKAGE_STRENGTH},
        {"label": "usage_only", "window": DEFAULT_WINDOW, "use_usage": True, "usage_alpha": best_alpha},
        both_on_variant,
    ]
    interaction_results = compare_variants(
        game_logs, scoring_config, SEASON, TUNING_WEEKS, interaction_variants,
        baseline_by_week_by_player=baseline_by_week_by_player,
    )
    for label, metrics in interaction_results.items():
        print_report(label, metrics)


if __name__ == "__main__":
    main()
