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
1. Edit SEASON / POSITIONS / TUNING_WEEKS / HOLDOUT_WEEKS /
   WINDOW_SIZES_TO_SWEEP below. main() loads the full active pool for
   POSITIONS via load_full_pool_game_logs() -- every player nflreadpy has a
   SEASON stat line for, not a hand-picked list -- so there's no per-player
   sample to curate.
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

import baseline
import usage
from matchup import compute_opponent_multiplier
from projections import DEFAULT_DECAY, DEFAULT_SHRINKAGE_STRENGTH, DEFAULT_WINDOW, project_player
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
        game_log, scoring_config, season, week, window=window, opponent_multiplier=opponent_multiplier, decay=decay,
        positional_baseline=positional_baseline, shrinkage_strength=shrinkage_strength,
        usage_multiplier=usage_multiplier,
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
    decay: float = DEFAULT_DECAY,
    use_shrinkage: bool = False,
    baseline_by_week: Optional[dict[int, float]] = None,
    shrinkage_strength: float = DEFAULT_SHRINKAGE_STRENGTH,
    use_usage: bool = False,
    usage_alpha: float = usage.DEFAULT_USAGE_ALPHA,
    usage_metric: str = usage.DEFAULT_USAGE_METRIC,
) -> list[dict[str, Any]]:
    """Run evaluate_player_week across a list of weeks for one player.
    Weeks with no recorded result (byes, weeks outside the loaded data)
    are silently skipped -- see evaluate_player_week. The same applies to
    a missing opponent lookup for a given week when use_matchup=True: a
    bye week has no opponent_team entry either, and NFL byes commonly fall
    within a mid-season tuning range (weeks 5-14) -- that's an expected
    per-week gap, not a misconfiguration, so it's skipped the same way a
    missing actual result is, not raised.

    A week missing from `baseline_by_week` when use_shrinkage=True is
    handled differently: unlike a missing opponent (a bye -- the week
    genuinely didn't happen), a missing baseline just means the pool had no
    eligible data for that position/week yet (e.g. before any prior-season
    fallback exists). The week is still graded, with positional_baseline=
    None -- i.e. project_player's normal no-shrinkage behavior for that one
    row -- rather than skipped, so a shrinkage sweep doesn't quietly lose
    rows a non-shrinkage variant would still grade (which would break
    paired_variant_metric's pairing).

    Raises ValueError only for the real misconfiguration case:
    use_matchup=True with no schedule_games at all, or use_shrinkage=True
    with baseline_by_week never supplied (None) -- nothing to look up for
    any week. An empty dict ({}) is deliberately NOT treated the same as
    None here: baseline.baselines_by_player_week_for_shrinkage legitimately
    returns {} for a player who's "full" tier for every requested week
    (nothing to shrink, not a misconfiguration) -- see
    tests/test_baseline.py's test_full_player_is_omitted. Those fail loud
    rather than silently producing an all-neutral run; this doesn't.

    use_usage=True needs no external lookup table and so has no equivalent
    misconfiguration case: unlike matchup (a different data source,
    schedule_games) or shrinkage (a pool-level aggregate, baseline_by_week),
    usage.compute_usage_multiplier reads entirely from this player's own
    game_log -- computed inline per week below.
    """
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
                continue  # no opponent recorded this week (e.g. a bye) -- nothing to grade
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
    baseline_by_week_by_player: Optional[dict[str, dict[int, float]]] = None,
) -> dict[str, dict[str, Any]]:
    """Run the full player pool through each variant config and return
    aggregated metrics per variant -- a window-size sweep or a
    matchup-on/off ablation is one function call, not hand-copied loops.

    Each variant: {"label": str, "window": int, "use_matchup": bool,
    "decay": float, "use_shrinkage": bool, "shrinkage_strength": float,
    "use_usage": bool, "usage_alpha": float, "usage_metric": str}.
    "decay"/"shrinkage_strength"/"usage_alpha"/"usage_metric" are optional
    per variant, defaulting the same as project_player/backtest_player.
    opponent_by_week_by_player: {player_id: {week: opponent_team}}, only
    required if any variant sets use_matchup=True.
    baseline_by_week_by_player: {player_id: {week: baseline}}, mirrors
    opponent_by_week_by_player's shape, only required if any variant sets
    use_shrinkage=True (see baseline.baselines_by_player_week). No
    equivalent lookup table parameter exists for use_usage=True -- usage
    is computed entirely from each player's own game_log inside
    backtest_player, no external data source needed.
    """
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
    """Run two variants and return matched (values_a, values_b) for one
    per-row metric ("abs_error" or "error"), restricted to (player, week)
    pairs graded by BOTH variants -- feeds paired_significance_test, which
    requires same-length, same-ordered samples.

    A variant can grade a different set of weeks than another for the same
    player (e.g. use_matchup=True skips a week with no opponent_by_week
    entry, see backtest_player's docstring, while use_matchup=False
    wouldn't skip that same week). Weeks only graded by one side of the
    comparison are dropped from both rather than left mismatched.

    confidence_filter: restrict the paired sample to one confidence tier
    ("no_data"/"low"/"full"), e.g. to test a shrinkage variant's effect on
    just the "low" tier rather than diluting it across a pooled sample
    where that tier is a small minority of rows. Shrinkage doesn't change
    games_used/confidence (see projections.project_player), so variant_a
    and variant_b agree on confidence for the same (player, week) as long
    as they share the same `window` -- true for every call site in this
    file today (shrinkage/matchup/decay ablations all hold window fixed),
    but not guaranteed in general if a future caller pairs two different
    window values under a confidence_filter; only row_a's confidence is
    consulted, so that combination would silently filter on variant_a's
    tier only.
    """
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
# Findings doc "Round 2" conclusion: the 19-player hand-picked archetype
# pool (established stars, breakout rookies, a committee back, a couple of
# QBs) was enough to firm up the window/decay/matchup calls, but it's small
# and curated -- not a sample the by_confidence / by-position breakdowns can
# be trusted against. POSITIONS below drives load_full_pool_game_logs(),
# which pulls every QB/RB/WR/TE nflreadpy has a 2025 stat line for (~600
# players) instead of a hand-picked list -- a full census, not a random
# subsample, so there's no sampling choice left to second-guess.
POSITIONS = ("QB", "RB", "WR", "TE")
TUNING_WEEKS = list(range(5, 15))    # weeks 5-14: tune window size / matchup on here
HOLDOUT_WEEKS = list(range(15, 19))  # weeks 15-18: confirm the choice generalizes here
WINDOW_SIZES_TO_SWEEP = [3, 4, 5, 6]
# Findings doc "ideas for improving accuracy" #1: candidate exponential
# decay factors to sweep against the DEFAULT_DECAY=1.0 (unweighted mean)
# baseline, at DEFAULT_WINDOW. 1.0 included so the sweep table itself shows
# the baseline alongside the candidates, not just implied by omission.
DECAY_VALUES_TO_SWEEP = [1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6]
# Round 4 (docs/research/projection-model-backtest-findings.md): candidate
# blend strengths for the "low" tier's shrinkage toward a positional
# baseline (see baseline.py / projections._shrinkage_weight). The "no_data"
# tier is unaffected by this sweep -- games_used==0 always takes the
# baseline in full regardless of strength, see _shrinkage_weight's
# docstring -- so its evaluation is a single baseline-on/off ablation, not
# part of this sweep.
SHRINKAGE_STRENGTHS_TO_SWEEP = [0.0, 0.25, 0.5, 0.75, 1.0]
# Round 4, idea 3: candidate exponents for usage.py's recent-vs-baseline
# usage-trend multiplier. alpha=0 (no adjustment) is included as the
# baseline, matching DECAY_VALUES_TO_SWEEP's/SHRINKAGE_STRENGTHS_TO_SWEEP's
# own convention.
USAGE_ALPHAS_TO_SWEEP = [0.0, 0.25, 0.5, 0.75, 1.0]


def load_full_pool_game_logs(season: int, positions: tuple[str, ...] = POSITIONS) -> dict[str, list[dict[str, Any]]]:
    """Every player at `positions` nflreadpy has a stat line for in
    `season` -- the full active pool, not a hand-picked list. One network
    call total (unlike the old per-player load_player_game_log, which
    called nfl.load_player_stats() once per player_id -- fine at 19 players,
    would have re-fetched the entire season's stats table ~600 times at
    full-pool scale), then grouped locally by player_id.
    """
    import nflreadpy as nfl  # local import: optional/local-only dependency

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
        # Not a scoring category (ignored by compute_league_points, same as
        # opponent_team above) -- carried through so main()'s
        # position-specific window sweep (findings doc idea #4) can segment
        # the player pool without a separate roster join.
        stat_line["position"] = row.get("position")
        # Also not a scoring category -- usage.py's compute_usage_multiplier
        # reads these directly off the game log (Round 4, idea 3).
        for col in usage.USAGE_COLUMNS:
            stat_line[col] = row.get(col)
        game_logs.setdefault(row["player_id"], []).append(stat_line)
    return game_logs


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

    # Round 1/2 (19-player pool) called the window-size choice by eyeballing
    # the sweep table above -- every other "does X help" question in this
    # file gets a real paired significance test, this one never did. Full
    # pool has enough n to actually run it: pick the tuning-weeks MAE winner
    # among the non-default candidates the same mechanical way
    # best_decay_label is picked below, then test it against DEFAULT_WINDOW
    # on both tuning and holdout weeks. Reuses tuning_window_results from
    # the sweep above rather than recomputing it.
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

    # best_window beating DEFAULT_WINDOW doesn't establish it's the single
    # best window -- the runner-up candidate (by tuning MAE) could be
    # statistically indistinguishable from it. Test that directly instead
    # of treating the sweep table's ranking as precise.
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

    # -----------------------------------------------------------------
    # Findings doc "ideas for improving accuracy" #1: does a recency-
    # decayed average beat the plain unweighted mean (decay=1.0)? Same
    # tuning/holdout-weeks discipline as the window sweep above -- a decay
    # factor is only trustworthy if it also holds up on weeks it wasn't
    # picked on.
    # -----------------------------------------------------------------
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

    # Best candidate by tuning-weeks MAE, excluding the decay=1.0 baseline
    # itself -- picked programmatically so this stays a mechanical
    # tuning-weeks decision, same discipline as the window-size sweep
    # above; still confirmed against holdout below, and against the
    # unweighted baseline via a real significance test, not just "lowest
    # number wins."
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

    # -----------------------------------------------------------------
    # Findings doc idea #4: does the ideal window size differ by position?
    # Segments the same pooled player list by `position` (carried on each
    # game-log entry by load_full_pool_game_logs above) and re-runs the
    # window sweep per position, tuning weeks only -- an exploratory check
    # per the findings doc ("cheap to test"), not a full tuning/holdout
    # split per position.
    # -----------------------------------------------------------------
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

        # Per-position sample sizes here (n_players in the single digits,
        # n graded weeks in the 20s-70s) are small enough that "window=6
        # has the lowest MAE for RBs" could easily be noise rather than a
        # real per-position effect -- same "0.005 difference" trap the
        # matchup ablation hit before it got a real significance test.
        # Check the apparent best window against DEFAULT_WINDOW the same
        # way, instead of eyeballing the table above.
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

    # -----------------------------------------------------------------
    # Round 4, idea 2: shrinkage toward a positional baseline for thin
    # samples. Judged bias-first on the tiers it targets (see the findings
    # doc) -- pooled MAE is checked as a guardrail, not the primary gate,
    # unlike every earlier round's window/decay/matchup calls.
    # -----------------------------------------------------------------
    print("\n" + "#" * 60)
    print("POSITIONAL BASELINES -- SANITY CHECK")
    print("#" * 60)
    all_weeks = sorted(set(TUNING_WEEKS) | set(HOLDOUT_WEEKS))
    # Two separately-scoped populations, per baseline.py's module docstring:
    # "debut" (games_played_before==0, excluding week 1 via
    # DEFAULT_DEBUT_MIN_WEEK -- the whole-league roster-debut week is a
    # different population from a genuine in-season call-up) feeds the
    # no_data tier; "thin" (0..window-1, unchanged) feeds the low tier.
    # Both a pooled "thin"-for-everyone population AND an unfiltered
    # "debut" population (including week 1) were confirmed, in this exact
    # run, to badly overshoot the no_data tier -- see the module docstring
    # for both confounds and the actual numbers.
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

    # -----------------------------------------------------------------
    # Strength sweep -- only the "low" tier can vary with strength (see
    # SHRINKAGE_STRENGTHS_TO_SWEEP's comment above); by_confidence's "low"
    # row in each printed report is what this sweep is actually about.
    # -----------------------------------------------------------------
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

    # Bias-first pick: minimize |low-tier bias| on tuning weeks, not MAE --
    # per the findings doc's Round 4 ship criteria for this idea.
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

    # best_strength beating "off" doesn't establish it's the single best
    # strength -- same "check the runner-up directly" discipline the
    # window-size decision (Round 3) used, rather than trusting the sweep
    # table's tuning-weeks ranking at face value.
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

    # -----------------------------------------------------------------
    # Population comparator -- the debut+thin dual resolver (the default
    # above) vs a single flat "all" population applied to everyone -- to
    # empirically demonstrate the population choice matters, per
    # baseline.py's module docstring, rather than just asserting it.
    # -----------------------------------------------------------------
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

    # -----------------------------------------------------------------
    # Round 4, idea 3: usage/opportunity trend multiplier (usage.py).
    # MAE-first, unlike idea 2 -- there's no equivalent "literal predict
    # zero" bug forcing a bias-first framing here, so this stays on the
    # same bar window/decay/matchup used.
    # -----------------------------------------------------------------
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

    # MAE-first pick (unlike idea 2's bias-first pick) -- same programmatic
    # "excluding the alpha=0 baseline itself" idiom DECAY_VALUES_TO_SWEEP's
    # best_decay_label uses.
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

    # -----------------------------------------------------------------
    # Interaction check: shrinkage (idea 2, already shipped at
    # DEFAULT_SHRINKAGE_STRENGTH) and usage together vs. each alone, since
    # they can partially offset for a rising-role rookie (shrinkage pulls
    # the average down toward a thin-sample baseline, usage pushes it up).
    # Measuring the combined effect, not modeling an interaction term.
    # -----------------------------------------------------------------
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
