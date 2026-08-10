"""
Historical backtest / model-tuning harness.

Purpose: validate and tune the *mechanics* of the in-house projection model
(projections.py + matchup.py) against real 2025 results, while the real
2026 league scoring rules are still blocked on the commissioner. Read this
before trusting the numbers this script prints:

CAN tell you: whether a 3, 4, 5, or 6-game rolling window (projections.py's
`window`) tracks real performance better; whether the opponent win-record
multiplier (matchup.py) actually reduces error or just adds noise; whether
the model is systematically biased high or low; whether accuracy differs
meaningfully between the "full window" and "low confidence" (cold-start)
tiers -- exactly the breakdown CLAUDE.md's eval layer (Next Steps item 8)
wants long-term, just run early against 2025 instead of live 2026 data.

CANNOT tell you: real 2026 accuracy in absolute points. Every number here
is computed through scoring_config.placeholder.json, not the real league's
rules -- MAE and bias are only meaningful for comparing model variants
against EACH OTHER (same config on both sides of a comparison), not as a
promise about how close projections will be once real scoring lands.
Correlation is reported alongside MAE as a somewhat more config-robust
secondary signal, since relative player ranking tends to survive a linear
rescaling of point values better than absolute error does -- not a
replacement for MAE, an additional angle on the same results.

Workflow:
1. Edit SEASON / PLAYER_IDS / TUNING_WEEKS / HOLDOUT_WEEKS /
   WINDOW_SIZES_TO_SWEEP below.
2. Run locally: `python3 backtest.py` (needs nflreadpy + network, both
   confirmed unreachable from the Cowork cloud sandbox this was built in).
3. Read the TUNING WEEKS table to pick a window size / decide whether the
   matchup multiplier earns its keep. Then check that SAME choice against
   the HOLDOUT WEEKS table before committing -- a choice that only looks
   good on the weeks you tuned against is much less trustworthy than one
   that also holds up on weeks you didn't look at while choosing. This is
   a deliberately lightweight train/holdout split, not full
   cross-validation -- proportionate to a single-season, single-league
   portfolio project, not a research pipeline.
4. Repeat once there's more real season data, or once the real scoring
   config lands and it's worth re-tuning against it specifically.

Only covers QB/RB/WR/TE -- the positions scoring.py's nflreadpy column map
and today's placeholder config cover. DST scoring isn't built yet (see
backend/README.md), so defenses aren't included here.
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional

from matchup import compute_opponent_multiplier
from projections import DEFAULT_WINDOW, project_player
from scoring import compute_league_points, nflreadpy_row_to_stat_line


def evaluate_player_week(
    game_log: list[dict[str, Any]],
    scoring_config: dict[str, Any],
    season: int,
    week: int,
    window: int = DEFAULT_WINDOW,
    opponent_multiplier: float = 1.0,
) -> Optional[dict[str, Any]]:
    """Grade one player's projection against one already-known week.

    Returns None if there's no actual result for that (season, week) in
    game_log (a bye week, or a week outside the data loaded) -- can't
    grade what didn't happen, so that week is silently excluded from the
    caller's results rather than counted as a zero.
    """
    actual_entry = next((g for g in game_log if g["season"] == season and g["week"] == week), None)
    if actual_entry is None:
        return None

    projection = project_player(
        game_log, scoring_config, season, week, window=window, opponent_multiplier=opponent_multiplier
    )
    actual_points = float(compute_league_points(actual_entry, scoring_config))
    projected_points = projection["projected_points"]

    return {
        "season": season,
        "week": week,
        "projected": projected_points,
        "actual": actual_points,
        # Positive error = model underprojected (actual came in higher);
        # negative = model overprojected. Signed, so aggregate_metrics can
        # tell "always a bit low" apart from "randomly off both ways."
        "error": round(actual_points - projected_points, 2),
        "abs_error": round(abs(actual_points - projected_points), 2),
        "confidence": projection["confidence"],
        "games_used": projection["games_used"],
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
) -> list[dict[str, Any]]:
    """Run evaluate_player_week across a list of weeks for one player.
    Weeks with no recorded result (byes, weeks outside the loaded data)
    are silently skipped -- see evaluate_player_week. The same applies to
    a missing opponent lookup for a given week when use_matchup=True: a
    bye week has no opponent_team entry either, and NFL byes commonly fall
    within a mid-season tuning range (weeks 5-14) -- that's an expected
    per-week gap, not a misconfiguration, so it's skipped the same way a
    missing actual result is, not raised.

    Raises ValueError only for the real misconfiguration case:
    use_matchup=True with no schedule_games at all (nothing to compute any
    multiplier from, for any week) -- that one fails loud rather than
    silently producing an all-neutral-multiplier run.
    """
    if use_matchup and not schedule_games:
        raise ValueError("use_matchup=True requires schedule_games")

    results = []
    for week in weeks:
        multiplier = 1.0
        if use_matchup:
            opponent_team = (opponent_by_week or {}).get(week)
            if opponent_team is None:
                continue  # no opponent recorded this week (e.g. a bye) -- nothing to grade
            multiplier = compute_opponent_multiplier(
                schedule_games, opponent_team, season, week
            )["multiplier"]
        row = evaluate_player_week(game_log, scoring_config, season, week, window=window, opponent_multiplier=multiplier)
        if row is not None:
            results.append(row)
    return results


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _pearson_correlation(xs: list[float], ys: list[float]) -> Optional[float]:
    """Plain-stdlib Pearson correlation (no `statistics.correlation` --
    that's Python 3.10+ only, and the project's existing scripts/.venv is
    3.9). Returns None for a degenerate series (fewer than 2 points, or a
    constant series with zero variance) rather than raising or faking a
    number."""
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
    """Sample standard deviation (ddof=1, i.e. divides by n-1) -- plain
    stdlib for the same reason as _pearson_correlation above."""
    n = len(values)
    mean = _mean(values)
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def _standard_normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def paired_significance_test(a: list[float], b: list[float], alpha: float = 0.05) -> dict[str, Any]:
    """Is the difference between two variants (e.g. matchup=on vs off,
    same players/weeks) a real effect or just noise -- the question the
    backtest findings doc currently answers by eyeballing ("a 0.005
    difference -- noise"). `a` and `b` must be paired and same-ordered
    (index i in each list is the same player-week in both), e.g. from
    paired_variant_metric below.

    Uses a paired z-test on the per-pair differences (a[i] - b[i]) rather
    than scipy's exact paired t-test: this project deliberately carries no
    numpy/scipy dependency (see _pearson_correlation's docstring -- plain
    stdlib, matching the pinned Python 3.9 venv), and for n>=30 the
    t-distribution and the standard normal are close enough that the
    p-value doesn't change any call this backtest makes. Below n=30 the
    approximation gets rougher -- treat p-values from small samples as
    directional, not exact.
    """
    if len(a) != len(b):
        raise ValueError("paired_significance_test requires equal-length paired samples")
    n = len(a)
    if n < 2:
        return {"n": n, "mean_diff": None, "p_value": None, "significant": None}

    diffs = [x - y for x, y in zip(a, b)]
    mean_diff = _mean(diffs)
    std_diff = _std(diffs)

    if std_diff == 0:
        # Every pair differs by exactly the same amount -- there's no
        # variance to test the mean against, so a nonzero difference is as
        # significant as it gets, and a zero one trivially isn't.
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


def aggregate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a pooled list of evaluate_player_week rows (across
    however many players/weeks the caller passed in) into headline
    numbers -- MAE and bias (config-dependent, only meaningful for
    comparing variants against each other, see module docstring),
    correlation (a more config-robust secondary signal), and the same
    breakdown split by confidence tier per CLAUDE.md's eval-layer intent
    (Next Steps item 8: track accuracy separately by confidence)."""
    if not results:
        return {"n": 0, "mae": None, "bias": None, "correlation": None, "by_confidence": {}}

    abs_errors = [r["abs_error"] for r in results]
    errors = [r["error"] for r in results]
    projected = [r["projected"] for r in results]
    actual = [r["actual"] for r in results]

    by_confidence = {}
    for tier in sorted({r["confidence"] for r in results}):
        tier_rows = [r for r in results if r["confidence"] == tier]
        by_confidence[tier] = {
            "n": len(tier_rows),
            "mae": round(_mean([r["abs_error"] for r in tier_rows]), 3),
            "bias": round(_mean([r["error"] for r in tier_rows]), 3),
        }

    return {
        "n": len(results),
        "mae": round(_mean(abs_errors), 3),
        "bias": round(_mean(errors), 3),
        "correlation": _pearson_correlation(projected, actual),
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
) -> dict[str, dict[str, Any]]:
    """Run the full player pool through each variant config and return
    aggregated metrics per variant -- a window-size sweep or a
    matchup-on/off ablation is one function call, not hand-copied loops.

    Each variant: {"label": str, "window": int, "use_matchup": bool}.
    opponent_by_week_by_player: {player_id: {week: opponent_team}}, only
    required if any variant sets use_matchup=True.
    """
    out = {}
    for variant in variants:
        pooled: list[dict[str, Any]] = []
        for player_id, game_log in game_logs_by_player.items():
            opponent_by_week = (opponent_by_week_by_player or {}).get(player_id)
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
) -> tuple[list[float], list[float]]:
    """Run two variants and return matched (values_a, values_b) for one
    per-row metric ("abs_error" or "error"), restricted to (player, week)
    pairs graded by BOTH variants -- feeds paired_significance_test, which
    requires same-length, same-ordered samples.

    A variant can grade a different set of weeks than another for the same
    player (e.g. use_matchup=True skips a week with no opponent_by_week
    entry, see backtest_player's docstring, while use_matchup=False
    wouldn't skip that same week). Weeks only graded by one side of the
    comparison are dropped from both rather than left mismatched.
    """
    values_a: list[float] = []
    values_b: list[float] = []
    for player_id, game_log in game_logs_by_player.items():
        opponent_by_week = (opponent_by_week_by_player or {}).get(player_id)
        rows_a = {
            r["week"]: r[metric]
            for r in backtest_player(
                game_log, scoring_config, season, weeks,
                window=variant_a["window"], use_matchup=variant_a.get("use_matchup", False),
                schedule_games=schedule_games, opponent_by_week=opponent_by_week,
            )
        }
        rows_b = {
            r["week"]: r[metric]
            for r in backtest_player(
                game_log, scoring_config, season, weeks,
                window=variant_b["window"], use_matchup=variant_b.get("use_matchup", False),
                schedule_games=schedule_games, opponent_by_week=opponent_by_week,
            )
        }
        for week in sorted(set(rows_a) & set(rows_b)):
            values_a.append(rows_a[week])
            values_b.append(rows_b[week])
    return values_a, values_b


def print_report(label: str, metrics: dict[str, Any]) -> None:
    """Human-readable console summary -- this is a script meant to be run
    and read, not a library with a separate reporting layer."""
    print(f"\n=== {label} ===")
    if metrics["n"] == 0:
        print("  no graded weeks")
        return
    print(f"  n={metrics['n']}  MAE={metrics['mae']}  bias={metrics['bias']:+}  corr={metrics['correlation']}")
    for tier, tier_metrics in metrics["by_confidence"].items():
        print(f"    [{tier}] n={tier_metrics['n']}  MAE={tier_metrics['mae']}  bias={tier_metrics['bias']:+}")


# ---------------------------------------------------------------------------
# Local-only data loading and the default run -- NOT exercised by the test
# suite (network + nflreadpy required, confirmed unreachable from the
# Cowork cloud sandbox this was built in). Run this file locally.
# ---------------------------------------------------------------------------

SEASON = 2025
# Fill in real nflreadpy player_ids before running. Deliberately mix
# archetypes, not just top players: a weekly-consistent stud, a boom/bust
# player, a committee/timeshare back, a rookie, and ideally one player who
# took over a starting role mid-season -- that last case is the design
# spec's flagged "role-change discontinuity" failure mode, and the single
# most informative test case available (see the design spec, section 3.2).
PLAYER_IDS: list[str] = [
    "00-0035676",
    "00-0036963",
    "00-0040122",
    "00-0038542",
    "00-0032398",
    "00-0033280",
    "00-0034827",
    "00-0038933",
    "00-0039851",
    "00-0040129",
    "00-0040663",
    "00-0033106",
    "00-0038543",
    "00-0036945",
    "00-0036322",
    "00-0039849",
    "00-0039075",
    "00-0036139",
    "00-0030506",
]
TUNING_WEEKS = list(range(5, 15))    # weeks 5-14: tune window size / matchup on here
HOLDOUT_WEEKS = list(range(15, 19))  # weeks 15-18: confirm the choice generalizes here
WINDOW_SIZES_TO_SWEEP = [3, 4, 5, 6]


def load_player_game_log(player_id: str, season: int) -> list[dict[str, Any]]:
    import nflreadpy as nfl  # local import: optional/local-only dependency

    stats = nfl.load_player_stats(seasons=[season])
    df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
    df = df[df["player_id"] == player_id]

    game_log = []
    for _, row in df.iterrows():
        stat_line = nflreadpy_row_to_stat_line(row.to_dict())
        stat_line["season"] = season
        stat_line["week"] = int(row["week"])
        stat_line["opponent_team"] = row.get("opponent_team")
        game_log.append(stat_line)
    return game_log


def load_schedule_games(season: int) -> list[dict[str, Any]]:
    import nflreadpy as nfl  # local import: optional/local-only dependency

    # Also pull the prior season so matchup.compute_opponent_multiplier's
    # early-season fallback (fewer than 4 games played this season -> use
    # last season's final record) has something to fall back to.
    schedules = nfl.load_schedules(seasons=[season, season - 1])
    df = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
    cols = ["season", "week", "home_team", "away_team", "home_score", "away_score"]
    return df[cols].to_dict("records")


def main():
    if not PLAYER_IDS:
        print("Set PLAYER_IDS at the top of this file before running -- see the module docstring.")
        return

    with open("scoring_config.placeholder.json") as f:
        scoring_config = json.load(f)

    game_logs = {pid: load_player_game_log(pid, SEASON) for pid in PLAYER_IDS}

    print("#" * 60)
    print("WINDOW SIZE SWEEP -- TUNING WEEKS")
    print("#" * 60)
    window_variants = [{"label": f"window={w}", "window": w, "use_matchup": False} for w in WINDOW_SIZES_TO_SWEEP]
    for label, metrics in compare_variants(game_logs, scoring_config, SEASON, TUNING_WEEKS, window_variants).items():
        print_report(label, metrics)

    print("\n" + "#" * 60)
    print("WINDOW SIZE SWEEP -- HOLDOUT WEEKS (confirm before committing)")
    print("#" * 60)
    for label, metrics in compare_variants(game_logs, scoring_config, SEASON, HOLDOUT_WEEKS, window_variants).items():
        print_report(label, metrics)

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


if __name__ == "__main__":
    main()
