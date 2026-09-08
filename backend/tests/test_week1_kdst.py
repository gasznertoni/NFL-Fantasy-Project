"""Unit tests for week1_kdst.py's pure half (everything but the nflreadpy
adapters, per that module's own split). Hand-built game logs with known
expected output, same pattern as test_scoring.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from week1_kdst import (  # noqa: E402
    FALLBACK_SHRINKAGE_K,
    MIN_FIT_OBSERVATIONS,
    build_observations,
    entity_season_means,
    fit_shrinkage_k,
    shrink,
    week1_estimates,
)

CONFIG = {"linear": {"fg_made_0_39": 3, "pat_made": 1}}


class TestShrink(unittest.TestCase):
    def test_no_games_falls_all_the_way_to_the_positional_mean(self):
        self.assertEqual(shrink(99.0, 0, 8.0, 15.0), 8.0)

    def test_k_zero_keeps_the_entity_average_untouched(self):
        self.assertEqual(shrink(12.0, 17, 8.0, 0.0), 12.0)

    def test_weight_is_n_over_n_plus_k(self):
        # 17/(17+3) = 0.85 on the entity, 0.15 on the position.
        self.assertAlmostEqual(shrink(10.0, 17, 5.0, 3.0), 0.85 * 10.0 + 0.15 * 5.0)

    def test_more_games_means_less_shrinkage(self):
        near = shrink(12.0, 17, 8.0, 15.0)
        far = shrink(12.0, 4, 8.0, 15.0)
        self.assertGreater(near, far)

    def test_a_kicker_is_pulled_harder_than_a_defence(self):
        # The fitted constants differ by roughly 4x, which is the finding: a
        # kicker's prior season says much less about him than a defence's does.
        k = shrink(12.0, 17, 8.0, FALLBACK_SHRINKAGE_K["K"])
        dst = shrink(12.0, 17, 8.0, FALLBACK_SHRINKAGE_K["DST"])
        self.assertLess(k, dst)


class TestEntitySeasonMeans(unittest.TestCase):
    def test_means_are_scored_through_the_league_config(self):
        logs = {"kicker-a": [{"fg_made_0_39": 2, "pat_made": 1},
                             {"fg_made_0_39": 1, "pat_made": 3},
                             {"fg_made_0_39": 0, "pat_made": 2},
                             {"fg_made_0_39": 3, "pat_made": 0}]}
        mean, games = entity_season_means(logs, CONFIG, "K")["kicker-a"]
        self.assertEqual(games, 4)
        self.assertAlmostEqual(mean, (7 + 6 + 2 + 9) / 4)

    def test_a_thin_prior_season_is_dropped_rather_than_trusted(self):
        logs = {"cameo": [{"fg_made_0_39": 1}, {"fg_made_0_39": 1}]}
        self.assertEqual(entity_season_means(logs, CONFIG, "K"), {})

    def test_the_min_games_floor_is_configurable(self):
        logs = {"cameo": [{"fg_made_0_39": 1}, {"fg_made_0_39": 1}]}
        self.assertIn("cameo", entity_season_means(logs, CONFIG, "K", min_games=2))


class TestBuildObservations(unittest.TestCase):
    def test_a_prior_season_is_paired_with_the_next_seasons_week_one(self):
        means = {2024: {"a": (10.0, 17)}}
        actuals = {2025: {"a": 14.0}}
        self.assertEqual(build_observations(means, actuals), [(10.0, 17, 14.0)])

    def test_an_entity_without_both_halves_is_skipped(self):
        means = {2024: {"a": (10.0, 17)}}
        actuals = {2025: {"a": 14.0, "rookie": 9.0}}
        self.assertEqual(len(build_observations(means, actuals)), 1)

    def test_a_season_with_no_prior_is_skipped(self):
        self.assertEqual(build_observations({2024: {"a": (10.0, 17)}}, {2024: {"a": 5.0}}), [])


class TestFitShrinkageK(unittest.TestCase):
    def test_too_few_rows_returns_none_rather_than_a_fragile_fit(self):
        rows = [(10.0, 17, 12.0)] * (MIN_FIT_OBSERVATIONS - 1)
        self.assertIsNone(fit_shrinkage_k(rows, 8.0))

    def test_a_perfectly_persistent_signal_fits_near_zero_shrinkage(self):
        # Week 1 always equals the prior mean -> trust the entity entirely.
        rows = [(float(i), 17, float(i)) for i in range(5, 45)]
        self.assertEqual(fit_shrinkage_k(rows, 20.0), 0.0)

    def test_pure_noise_fits_heavy_shrinkage(self):
        # Prior carries no information about week 1: everyone scored the mean.
        rows = [(float(i), 17, 20.0) for i in range(5, 45)]
        self.assertGreater(fit_shrinkage_k(rows, 20.0), 20.0)


class TestWeek1Estimates(unittest.TestCase):
    def test_estimates_are_produced_per_entity(self):
        out = week1_estimates({"a": (12.0, 17), "b": (5.0, 17)}, 8.0, 3.0)
        self.assertGreater(out["a"], out["b"])
        self.assertEqual(sorted(out), ["a", "b"])

    def test_an_entity_with_no_prior_is_simply_absent(self):
        # The caller then falls back to the flat positional baseline for it,
        # which is the pre-2026-09-06 behaviour for everyone.
        self.assertNotIn("rookie", week1_estimates({"a": (12.0, 17)}, 8.0, 3.0))

    def test_estimates_differ_from_each_other(self):
        # The whole point: the flat baseline gave every entity one number.
        out = week1_estimates({c: (float(i + 4), 17) for i, c in enumerate("abcdef")}, 8.0, 3.0)
        self.assertEqual(len(set(out.values())), 6)


if __name__ == "__main__":
    unittest.main()
