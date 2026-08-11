"""Unit tests for usage.py: the trend-ratio multiplier, the alpha=0 no-op
contract, the NaN/negative-value traps confirmed against real 2025 data
(see the module docstring), and the no-signal gate (QBs, zero-target
receivers, missing data)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage import (  # noqa: E402
    MIN_USAGE_MULTIPLIER,
    compute_usage_multiplier,
    multiplier_from_usage_ratio,
)


def game(week, wopr=None, target_share=None):
    g = {"season": 2025, "week": week}
    if wopr is not None:
        g["wopr"] = wopr
    if target_share is not None:
        g["target_share"] = target_share
    return g


class TestMultiplierFromUsageRatio(unittest.TestCase):
    def test_alpha_zero_is_always_exactly_one(self):
        for ratio in [0.01, 0.5, 1.0, 1.8, 5.0, -15.0, 0.0]:
            self.assertEqual(multiplier_from_usage_ratio(ratio, alpha=0.0), 1.0)

    def test_rising_ratio_with_positive_alpha_is_above_one(self):
        self.assertGreater(multiplier_from_usage_ratio(1.8, alpha=0.5), 1.0)

    def test_falling_ratio_with_positive_alpha_is_below_one(self):
        self.assertLess(multiplier_from_usage_ratio(0.5, alpha=0.5), 1.0)

    def test_flat_ratio_is_exactly_one_regardless_of_alpha(self):
        self.assertEqual(multiplier_from_usage_ratio(1.0, alpha=0.75), 1.0)

    def test_extreme_high_ratio_binds_the_output_clamp(self):
        self.assertLessEqual(multiplier_from_usage_ratio(1000.0, alpha=1.0), 1.4)

    def test_negative_ratio_does_not_crash_and_binds_the_low_clamp(self):
        # The domain-error risk this test guards against: ratio**alpha for
        # a negative ratio and non-integer alpha would raise/go complex if
        # the ratio weren't floored to a positive value first.
        result = multiplier_from_usage_ratio(-15.0, alpha=0.5)
        self.assertEqual(result, MIN_USAGE_MULTIPLIER)


class TestComputeUsageMultiplier(unittest.TestCase):
    def test_rising_usage_gives_multiplier_above_one(self):
        # baseline (6 games) mean 0.1, recent (2 games) mean 0.3
        log = [game(w, wopr=0.1) for w in range(1, 5)] + [game(5, wopr=0.3), game(6, wopr=0.3)]
        result = compute_usage_multiplier(log, 2025, 7, alpha=0.5)
        self.assertAlmostEqual(result["baseline_usage"], 1 / 6, places=3)
        self.assertAlmostEqual(result["recent_usage"], 0.3)
        self.assertGreater(result["ratio"], 1.0)
        self.assertGreater(result["multiplier"], 1.0)
        self.assertEqual(result["source"], "current_season")

    def test_falling_usage_gives_multiplier_below_one(self):
        log = [game(w, wopr=0.3) for w in range(1, 5)] + [game(5, wopr=0.05), game(6, wopr=0.05)]
        result = compute_usage_multiplier(log, 2025, 7, alpha=0.5)
        self.assertLess(result["ratio"], 1.0)
        self.assertLess(result["multiplier"], 1.0)

    def test_alpha_zero_is_one_even_with_a_real_trend(self):
        log = [game(w, wopr=0.1) for w in range(1, 5)] + [game(5, wopr=0.9), game(6, wopr=0.9)]
        result = compute_usage_multiplier(log, 2025, 7, alpha=0.0)
        self.assertEqual(result["multiplier"], 1.0)

    def test_as_of_discipline_excludes_the_target_week_itself(self):
        log = [game(w, wopr=0.2) for w in range(1, 4)] + [game(4, wopr=0.99)]
        # projecting week 4 must not see week 4's own (huge) usage value
        result = compute_usage_multiplier(log, 2025, 4, alpha=0.5, recent_games=1, baseline_games=3)
        self.assertAlmostEqual(result["recent_usage"], 0.2)

    def test_no_games_at_all_is_no_usage_data(self):
        result = compute_usage_multiplier([], 2025, 1, alpha=0.5)
        self.assertEqual(result["source"], "no_usage_data")
        self.assertEqual(result["multiplier"], 1.0)

    def test_near_zero_baseline_is_no_usage_data(self):
        # QB-like: wopr present but always ~0 -- confirmed against real
        # 2025 data (module docstring), not a hypothetical case.
        log = [game(w, wopr=0.0) for w in range(1, 5)]
        result = compute_usage_multiplier(log, 2025, 5, alpha=0.5)
        self.assertEqual(result["source"], "no_usage_data")
        self.assertEqual(result["multiplier"], 1.0)

    def test_missing_metric_value_is_skipped_not_a_crash(self):
        log = [game(1), game(2, wopr=0.2), game(3, wopr=0.2)]  # week 1 has no wopr key at all
        result = compute_usage_multiplier(log, 2025, 4, alpha=0.5)
        self.assertAlmostEqual(result["baseline_usage"], 0.2)

    def test_nan_metric_value_is_treated_as_missing_not_nan(self):
        log = [game(1, wopr=float("nan")), game(2, wopr=0.2), game(3, wopr=0.2)]
        result = compute_usage_multiplier(log, 2025, 4, alpha=0.5)
        # regression guard: a NaN must not propagate into baseline_usage or
        # the multiplier (NaN != NaN, so assertEqual would also catch this,
        # but assert the actual clean value explicitly)
        self.assertAlmostEqual(result["baseline_usage"], 0.2)
        self.assertEqual(result["multiplier"], result["multiplier"])  # NaN != NaN

    def test_negative_baseline_usage_is_no_usage_data(self):
        # baseline mean is negative (below MIN_BASELINE_USAGE) -- must gate
        # to neutral rather than feed a negative baseline into a ratio.
        log = [game(w, wopr=-0.3) for w in range(1, 5)]
        result = compute_usage_multiplier(log, 2025, 5, alpha=0.5)
        self.assertEqual(result["source"], "no_usage_data")
        self.assertEqual(result["multiplier"], 1.0)

    def test_positive_baseline_negative_recent_does_not_crash(self):
        # The exact shape confirmed possible in real 2025 data (module
        # docstring): baseline positive, a recent negative dip -- ratio
        # goes deeply negative, must not raise.
        log = [game(w, wopr=0.3) for w in range(1, 5)] + [game(5, wopr=-0.5), game(6, wopr=-0.5)]
        for alpha in (0.0, 0.25, 0.5, 0.75, 1.0):
            result = compute_usage_multiplier(log, 2025, 7, alpha=alpha, recent_games=2, baseline_games=6)
            self.assertEqual(result["source"], "current_season")
            self.assertGreaterEqual(result["multiplier"], MIN_USAGE_MULTIPLIER)

    def test_metric_parameter_reads_target_share(self):
        log = [game(w, target_share=0.1) for w in range(1, 5)] + [game(5, target_share=0.4), game(6, target_share=0.4)]
        result = compute_usage_multiplier(log, 2025, 7, metric="target_share", alpha=0.5)
        self.assertAlmostEqual(result["recent_usage"], 0.4)
        self.assertGreater(result["multiplier"], 1.0)


if __name__ == "__main__":
    unittest.main()
