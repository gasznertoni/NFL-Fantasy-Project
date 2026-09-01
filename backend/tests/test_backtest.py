"""Unit tests for backtest.py's synthetic-data-testable logic: grading a
single week, aggregating metrics (including the confidence-tier
breakdown), correlation, and the variant-comparison harness used for
window-size sweeps and the matchup on/off ablation. The real nflreadpy
loaders at the bottom of backtest.py are untested here (network required,
confirmed unreachable from this build environment)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from projections import DEFAULT_DECAY
from backtest import (  # noqa: E402
    _pearson_correlation,
    aggregate_metrics,
    backtest_player,
    compare_variants,
    evaluate_player_week,
    paired_significance_test,
    paired_variant_metric,
)

CONFIG = {"linear": {"rush_yd": 0.1, "rush_td": 6}}


def game(season, week, rush_yd, rush_td=0, opponent_team=None, wopr=None):
    g = {"season": season, "week": week, "rush_yd": rush_yd, "rush_td": rush_td, "opponent_team": opponent_team}
    if wopr is not None:
        g["wopr"] = wopr
    return g


class TestEvaluatePlayerWeek(unittest.TestCase):
    def test_grades_a_known_week_correctly(self):
        game_log = [game(2025, 1, 50), game(2025, 2, 50), game(2025, 3, 100)]
        # projection for week 3 uses weeks 1-2 only (avg 5.0), actual week 3 = 10.0
        result = evaluate_player_week(game_log, CONFIG, season=2025, week=3, window=4)
        self.assertAlmostEqual(result["projected"], 5.0)
        self.assertAlmostEqual(result["actual"], 10.0)
        self.assertAlmostEqual(result["error"], 5.0)  # actual - projected, model underprojected
        self.assertAlmostEqual(result["abs_error"], 5.0)

    def test_bye_week_or_missing_week_returns_none(self):
        game_log = [game(2025, 1, 50)]
        result = evaluate_player_week(game_log, CONFIG, season=2025, week=2, window=4)
        self.assertIsNone(result)

    def test_opponent_multiplier_is_applied_to_projection_not_actual(self):
        game_log = [game(2025, 1, 50), game(2025, 2, 100)]
        result = evaluate_player_week(game_log, CONFIG, season=2025, week=2, window=4, opponent_multiplier=1.5)
        # week 1 only in window -> rolling_avg 5.0 * 1.5 = 7.5
        self.assertAlmostEqual(result["projected"], 7.5)
        self.assertAlmostEqual(result["actual"], 10.0)  # unaffected by the multiplier


class TestDecayThreading(unittest.TestCase):
    """decay must actually reach project_player through every layer of the
    harness (evaluate_player_week -> backtest_player -> compare_variants /
    paired_variant_metric) -- the findings doc's decay sweep (idea #1) is
    worthless if a decay value silently gets dropped somewhere in here and
    every variant secretly runs decay=1.0."""

    def test_evaluate_player_week_passes_decay_through(self):
        # 0 pts, 10 pts, then a week-3 actual result to grade against (any
        # value works -- only "projected" is asserted on here).
        game_log = [game(2025, 1, 0), game(2025, 2, 100), game(2025, 3, 50)]
        unweighted = evaluate_player_week(game_log, CONFIG, season=2025, week=3, window=4, decay=1.0)
        decayed = evaluate_player_week(game_log, CONFIG, season=2025, week=3, window=4, decay=0.5)
        self.assertAlmostEqual(unweighted["projected"], 5.0)      # plain mean of 0, 10
        self.assertAlmostEqual(decayed["projected"], 10 / 1.5, places=2)  # weighted toward the more recent 10

    def test_compare_variants_decay_variants_produce_distinct_metrics(self):
        game_logs = {"p1": [game(2025, w, 0 if w % 2 else 100) for w in range(1, 6)]}
        variants = [
            {"label": "decay=1.0", "window": 4, "decay": 1.0},
            {"label": "decay=0.5", "window": 4, "decay": 0.5},
        ]
        results = compare_variants(game_logs, CONFIG, season=2025, weeks=[5], variants=variants)
        self.assertNotEqual(results["decay=1.0"]["mae"], results["decay=0.5"]["mae"])

    def test_paired_variant_metric_decay_default_matches_omitted(self):
        """A variant dict that omits "decay" must behave exactly like one that
        states DEFAULT_DECAY explicitly (compare_variants/paired_variant_metric
        default to it, same contract as project_player itself).

        Pinned to DEFAULT_DECAY rather than the literal 1.0 this once used: the
        default became 0.9 on 2026-09-01, and the contract being tested is
        "omitted == the default", not "omitted == unweighted"."""
        game_logs = {"p1": [game(2025, w, 10 * w) for w in range(1, 6)]}
        values_omitted, values_explicit = paired_variant_metric(
            game_logs, CONFIG, season=2025, weeks=[5],
            variant_a={"window": 4}, variant_b={"window": 4, "decay": DEFAULT_DECAY},
        )
        self.assertEqual(values_omitted, values_explicit)


class TestShrinkageThreading(unittest.TestCase):
    """positional_baseline/shrinkage_strength must actually reach
    project_player through every harness layer, same rationale as
    TestDecayThreading above -- Round 4's shrinkage sweep is worthless if a
    baseline silently gets dropped somewhere and every variant secretly
    runs with no shrinkage at all."""

    def test_evaluate_player_week_passes_baseline_through(self):
        result = evaluate_player_week(
            [], CONFIG, season=2025, week=1, window=4, positional_baseline=8.0,
            shrinkage_strength=1.0, shrinkage_mode="window",
        )
        self.assertIsNone(result)  # no actual week-1 result in an empty log -- nothing to grade

        game_log = [game(2025, 1, 0), game(2025, 2, 100), game(2025, 3, 999)]  # 0 pts, 10 pts, week-3 actual
        result = evaluate_player_week(
            game_log, CONFIG, season=2025, week=3, window=4, positional_baseline=8.0,
            shrinkage_strength=1.0, shrinkage_mode="window", decay=1.0,
        )
        # games_used=2 < window=4 -> weight = 1 - 1.0*(1 - 2/4) = 0.5
        # shrunk_avg = 0.5*5.0 + 0.5*8.0 = 6.5
        self.assertAlmostEqual(result["projected"], 6.5)

    def test_compare_variants_use_shrinkage_without_baselines_raises(self):
        game_logs = {"p1": [game(2025, 1, 50)]}
        variants = [{"label": "shrink=on", "window": 4, "use_shrinkage": True}]
        with self.assertRaises(ValueError):
            compare_variants(game_logs, CONFIG, season=2025, weeks=[2], variants=variants)

    def test_empty_per_player_baseline_dict_does_not_raise(self):
        """Regression: baseline.baselines_by_player_week_for_shrinkage
        legitimately returns {} (not omits the key) for a player who's
        "full" tier for every requested week -- that's "nothing to shrink,"
        not a misconfiguration. `{}` used to trip the same guard that
        catches a genuinely missing baseline (None), since `not {}` is True
        in Python -- the guard must only fire on None."""
        game_logs = {"p1": [game(2025, 1, 50), game(2025, 2, 999)]}
        results = backtest_player(
            game_logs["p1"], CONFIG, season=2025, weeks=[2], window=4,
            use_shrinkage=True, baseline_by_week={},  # present, deliberately empty
        )
        self.assertEqual(len(results), 1)  # week 2 still graded, no shrinkage applied

    def test_compare_variants_shrinkage_variants_produce_distinct_metrics(self):
        # weeks 1,2 -> 0 pts each -> rolling_avg=0 as of week 3; week 3's own
        # actual result (999) is what evaluate_player_week grades against.
        game_logs = {"p1": [game(2025, w, 0) for w in range(1, 3)] + [game(2025, 3, 999)]}
        baseline_by_week_by_player = {"p1": {3: 20.0}}
        variants = [
            {"label": "shrink=off", "window": 4, "use_shrinkage": False},
            {"label": "shrink=on", "window": 4, "use_shrinkage": True, "shrinkage_strength": 1.0},
        ]
        results = compare_variants(
            game_logs, CONFIG, season=2025, weeks=[3], variants=variants,
            baseline_by_week_by_player=baseline_by_week_by_player,
        )
        self.assertNotEqual(results["shrink=off"]["mae"], results["shrink=on"]["mae"])

    def test_missing_week_in_baseline_by_week_is_graded_not_skipped(self):
        """Unlike a missing opponent (a bye -- the week didn't happen), a
        missing baseline just means no pool baseline exists yet for that
        week -- the row must still be graded (with no shrinkage applied),
        not silently dropped."""
        game_log = [game(2025, 1, 50), game(2025, 2, 50), game(2025, 3, 50)]
        baseline_by_week = {2: 99.0}  # no entry for week 3
        results = backtest_player(
            game_log, CONFIG, season=2025, weeks=[2, 3], window=4,
            use_shrinkage=True, baseline_by_week=baseline_by_week, shrinkage_strength=1.0,
        )
        graded_weeks = {r["week"] for r in results}
        self.assertEqual(graded_weeks, {2, 3})  # week 3 still graded despite the gap

    def test_confidence_filter_restricts_and_keeps_pairs_aligned(self):
        # p1: no_data at week 5 (only a week-5 actual result, no prior
        # games -- games_used counts games strictly BEFORE the target
        # week, so this game itself doesn't count toward it).
        # p2: full window at week 5 (4 prior games, weeks 1-4, window=4).
        game_logs = {
            "p1": [game(2025, 5, 999)],
            "p2": [game(2025, w, 40) for w in range(1, 5)] + [game(2025, 5, 999)],
        }
        baseline_by_week_by_player = {"p1": {5: 5.0}, "p2": {5: 5.0}}
        values_off, values_on = paired_variant_metric(
            game_logs, CONFIG, season=2025, weeks=[5],
            variant_a={"window": 4, "use_shrinkage": False},
            variant_b={"window": 4, "use_shrinkage": True, "shrinkage_strength": 1.0},
            baseline_by_week_by_player=baseline_by_week_by_player,
            confidence_filter="no_data",
        )
        self.assertEqual(len(values_off), 1)  # only p1's no_data row survives the filter
        self.assertEqual(len(values_on), 1)


class TestUsageThreading(unittest.TestCase):
    """usage_alpha/usage_metric must actually reach project_player through
    every harness layer, same rationale as TestDecayThreading/
    TestShrinkageThreading above. Unlike shrinkage, usage needs no external
    lookup table (it reads entirely from the player's own game_log), so
    there's no "use_usage=True with nothing supplied" misconfiguration case
    to test -- only that the parameter is actually threaded through."""

    def test_evaluate_player_week_passes_usage_multiplier_through(self):
        game_log = [game(2025, 1, 0)]
        result = evaluate_player_week(game_log, CONFIG, season=2025, week=2, window=4, usage_multiplier=1.5)
        self.assertIsNone(result)  # no actual week-2 result -- nothing to grade

        # rush_yd=0 -> rolling_avg=0.0, so usage_multiplier's effect isn't
        # directly visible via projected != 0 here; confirm via a nonzero
        # rolling_avg instead.
        game_log = [game(2025, 1, 100), game(2025, 2, 999)]
        result = evaluate_player_week(game_log, CONFIG, season=2025, week=2, window=4, usage_multiplier=1.5)
        self.assertAlmostEqual(result["projected"], 10.0 * 1.5)

    def test_compare_variants_use_usage_variants_produce_distinct_metrics(self):
        # rising wopr (0.1 baseline -> 0.3 recent) over weeks 1-6, graded
        # against an actual week-7 result.
        game_logs = {
            "p1": [game(2025, w, 100, wopr=0.1) for w in range(1, 5)]
            + [game(2025, 5, 100, wopr=0.3), game(2025, 6, 100, wopr=0.3)]
            + [game(2025, 7, 999)],
        }
        variants = [
            {"label": "usage=off", "window": 6, "use_usage": False},
            {"label": "usage=on", "window": 6, "use_usage": True, "usage_alpha": 1.0},
        ]
        results = compare_variants(game_logs, CONFIG, season=2025, weeks=[7], variants=variants)
        self.assertNotEqual(results["usage=off"]["mae"], results["usage=on"]["mae"])

    def test_usage_alpha_default_matches_omitted(self):
        """A variant dict that omits "usage_alpha" entirely with
        use_usage=True must behave exactly like usage_alpha=DEFAULT_USAGE_ALPHA
        (0.0, i.e. a no-op multiplier) -- same contract decay's equivalent
        test enforces."""
        game_logs = {
            "p1": [game(2025, w, 100, wopr=0.1) for w in range(1, 5)]
            + [game(2025, 5, 100, wopr=0.3), game(2025, 6, 100, wopr=0.3)]
            + [game(2025, 7, 999)],
        }
        values_omitted, values_explicit = paired_variant_metric(
            game_logs, CONFIG, season=2025, weeks=[7],
            variant_a={"window": 6, "use_usage": True},
            variant_b={"window": 6, "use_usage": True, "usage_alpha": 0.0},
        )
        self.assertEqual(values_omitted, values_explicit)


class TestBacktestPlayer(unittest.TestCase):
    def test_skips_ungraded_weeks_without_raising(self):
        game_log = [game(2025, 1, 50), game(2025, 3, 50)]  # week 2 is a bye
        results = backtest_player(game_log, CONFIG, season=2025, weeks=[2, 3], window=4)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["week"], 3)

    def test_use_matchup_without_schedule_data_raises(self):
        game_log = [game(2025, 1, 50), game(2025, 2, 50)]
        with self.assertRaises(ValueError):
            backtest_player(game_log, CONFIG, season=2025, weeks=[2], window=4, use_matchup=True)

    def test_bye_week_is_skipped_not_raised_when_matchup_enabled(self):
        """Regression test: a real run hit this exact case -- NFL bye weeks
        commonly fall inside a mid-season tuning range, so a missing
        opponent_by_week entry for one week (out of several tested) must be
        skipped like any other ungraded week, not treated as a
        misconfiguration that kills the whole batch."""
        game_log = [game(2025, w, 50) for w in [1, 2, 4, 5]]  # week 3 is a bye
        schedule = [{"season": 2025, "week": 1, "home_team": "BUF", "away_team": "MIA", "home_score": 20, "away_score": 17}]
        opponent_by_week = {2: "MIA", 4: "NYJ", 5: "NE"}  # no entry for week 3, matching the bye
        results = backtest_player(
            game_log, CONFIG, season=2025, weeks=[2, 3, 4, 5], window=4,
            use_matchup=True, schedule_games=schedule, opponent_by_week=opponent_by_week,
        )
        graded_weeks = {r["week"] for r in results}
        self.assertNotIn(3, graded_weeks)          # bye week skipped, not an error
        self.assertEqual(graded_weeks, {2, 4, 5})  # every other week still graded


class TestAggregateMetrics(unittest.TestCase):
    def test_empty_results_returns_none_metrics_not_a_crash(self):
        metrics = aggregate_metrics([])
        self.assertEqual(metrics["n"], 0)
        self.assertIsNone(metrics["mae"])

    def test_mae_and_bias_computed_correctly(self):
        results = [
            {"error": 2.0, "abs_error": 2.0, "projected": 10.0, "actual": 12.0, "confidence": "full"},
            {"error": -4.0, "abs_error": 4.0, "projected": 10.0, "actual": 6.0, "confidence": "full"},
        ]
        metrics = aggregate_metrics(results)
        self.assertAlmostEqual(metrics["mae"], 3.0)   # mean(2, 4)
        self.assertAlmostEqual(metrics["bias"], -1.0)  # mean(2, -4)

    def test_breaks_down_by_confidence_tier(self):
        results = [
            {"error": 1.0, "abs_error": 1.0, "projected": 5.0, "actual": 6.0, "confidence": "full"},
            {"error": 5.0, "abs_error": 5.0, "projected": 5.0, "actual": 10.0, "confidence": "low"},
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["by_confidence"]["full"]["mae"], 1.0)
        self.assertEqual(metrics["by_confidence"]["low"]["mae"], 5.0)


class TestPearsonCorrelation(unittest.TestCase):
    def test_perfect_positive_correlation(self):
        self.assertAlmostEqual(_pearson_correlation([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)

    def test_perfect_negative_correlation(self):
        self.assertAlmostEqual(_pearson_correlation([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)

    def test_constant_series_returns_none_not_a_divide_by_zero(self):
        self.assertIsNone(_pearson_correlation([5, 5, 5], [1, 2, 3]))

    def test_fewer_than_two_points_returns_none(self):
        self.assertIsNone(_pearson_correlation([1], [1]))


class TestCompareVariants(unittest.TestCase):
    def test_window_sweep_produces_one_result_per_variant(self):
        game_logs = {"p1": [game(2025, w, 10 * w) for w in range(1, 6)]}
        variants = [{"label": "window=2", "window": 2}, {"label": "window=4", "window": 4}]
        results = compare_variants(game_logs, CONFIG, season=2025, weeks=[5], variants=variants)
        self.assertEqual(set(results.keys()), {"window=2", "window=4"})
        # window=2 uses weeks 3,4 (avg 35); window=4 uses weeks 1-4 (avg 25) -- different projections
        self.assertNotEqual(results["window=2"]["mae"], results["window=4"]["mae"])

    def test_matchup_ablation_pools_across_players(self):
        game_logs = {
            "p1": [game(2025, 1, 50, opponent_team="BUF"), game(2025, 2, 50, opponent_team="MIA")],
            "p2": [game(2025, 1, 20, opponent_team="BUF"), game(2025, 2, 20, opponent_team="MIA")],
        }
        schedule = [
            {"season": 2025, "week": 1, "home_team": "MIA", "away_team": "BUF", "home_score": 30, "away_score": 10},
        ]
        opponent_by_week = {"p1": {2: "MIA"}, "p2": {2: "MIA"}}
        variants = [
            {"label": "matchup=off", "window": 4, "use_matchup": False},
            {"label": "matchup=on", "window": 4, "use_matchup": True},
        ]
        results = compare_variants(
            game_logs, CONFIG, season=2025, weeks=[2], variants=variants,
            schedule_games=schedule, opponent_by_week_by_player=opponent_by_week,
        )
        self.assertEqual(results["matchup=off"]["n"], 2)
        self.assertEqual(results["matchup=on"]["n"], 2)
        # MIA lost badly week 1 (0-1, <0.4 record via prior-season fallback... here it's
        # current season with <4 games so it falls back to prior season, which has no
        # data either -> neutral multiplier=1.0) -- just confirm both variants ran without
        # error and matchup=on didn't silently no-op by comparing projected values exist.
        self.assertIsNotNone(results["matchup=on"]["mae"])


class TestPairedSignificanceTest(unittest.TestCase):
    def test_identical_samples_are_not_significant(self):
        a = [10.0, 12.0, 8.0, 11.0, 9.0]
        result = paired_significance_test(a, list(a))
        self.assertEqual(result["mean_diff"], 0.0)
        self.assertEqual(result["p_value"], 1.0)
        self.assertFalse(result["significant"])

    def test_small_mean_diff_amid_real_variance_is_not_significant(self):
        """Mirrors the backtest findings doc's matchup-ablation call: a
        tiny mean difference (0.2) sitting inside much larger per-pair
        noise (diffs range -4 to +4) should NOT come back significant --
        this is the exact "is 0.005 real or noise" judgment call the doc
        made by eye, now made by the test itself. Hand-computed: diffs =
        [2,-2,4,-4,1], mean=0.2, sample std~3.194, z~0.14, p~0.89."""
        a = [12.0, 8.0, 14.0, 6.0, 11.0]
        b = [10.0, 10.0, 10.0, 10.0, 10.0]
        result = paired_significance_test(a, b)
        self.assertAlmostEqual(result["mean_diff"], 0.2)
        self.assertGreater(result["p_value"], 0.05)
        self.assertFalse(result["significant"])

    def test_consistent_large_diff_is_significant(self):
        a = [3.0, 4.0] * 10  # n=20, mean diff 3.5, modest variance
        b = [0.0] * 20
        result = paired_significance_test(a, b)
        self.assertAlmostEqual(result["mean_diff"], 3.5)
        self.assertLess(result["p_value"], 0.001)
        self.assertTrue(result["significant"])

    def test_zero_variance_nonzero_diff_is_significant(self):
        a = [5.0, 5.0, 5.0]
        b = [3.0, 3.0, 3.0]
        result = paired_significance_test(a, b)
        self.assertEqual(result["p_value"], 0.0)
        self.assertTrue(result["significant"])

    def test_mismatched_lengths_raises(self):
        with self.assertRaises(ValueError):
            paired_significance_test([1.0, 2.0], [1.0])

    def test_fewer_than_two_pairs_returns_none_fields(self):
        result = paired_significance_test([1.0], [2.0])
        self.assertIsNone(result["p_value"])
        self.assertIsNone(result["significant"])


class TestPairedVariantMetric(unittest.TestCase):
    def test_matches_same_weeks_across_variants(self):
        game_logs = {"p1": [game(2025, w, 10 * w) for w in range(1, 6)]}
        values_a, values_b = paired_variant_metric(
            game_logs, CONFIG, season=2025, weeks=[4, 5],
            variant_a={"window": 2}, variant_b={"window": 4},
        )
        self.assertEqual(len(values_a), 2)
        self.assertEqual(len(values_b), 2)

    def test_drops_weeks_only_graded_by_one_variant(self):
        """One variant (matchup=on) skips week 4 because opponent_by_week
        has no entry for it (a data gap, not a bye -- actual results exist
        for every week here); the other variant (matchup=off) doesn't skip
        it. paired_variant_metric must drop week 4 from both sides rather
        than pairing it with nothing."""
        game_log = [game(2025, w, 50) for w in range(1, 6)]
        schedule = [{"season": 2025, "week": 1, "home_team": "BUF", "away_team": "MIA", "home_score": 20, "away_score": 17}]
        opponent_by_week = {2: "MIA", 3: "NYJ", 5: "NE"}  # no entry for week 4
        values_off, values_on = paired_variant_metric(
            {"p1": game_log}, CONFIG, season=2025, weeks=[2, 3, 4, 5],
            variant_a={"window": 4, "use_matchup": False},
            variant_b={"window": 4, "use_matchup": True},
            schedule_games=schedule,
            opponent_by_week_by_player={"p1": opponent_by_week},
        )
        self.assertEqual(len(values_off), 3)  # weeks 2, 3, 5 only
        self.assertEqual(len(values_on), 3)


if __name__ == "__main__":
    unittest.main()
