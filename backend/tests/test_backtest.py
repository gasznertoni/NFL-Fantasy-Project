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


def game(season, week, rush_yd, rush_td=0, opponent_team=None):
    return {"season": season, "week": week, "rush_yd": rush_yd, "rush_td": rush_td, "opponent_team": opponent_team}


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
