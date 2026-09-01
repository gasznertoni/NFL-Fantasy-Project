"""Unit tests for projections.py, per design spec section 6: a synthetic
game log including a bye week and a rookie's first game, confirming
"games played, not weeks elapsed" (section 3.2) and the as-of/cold-start
behavior actually work as described."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from projections import project_player, project_players  # noqa: E402

CONFIG = {"linear": {"rush_yd": 0.1, "rush_td": 6}}


def game(season, week, rush_yd, rush_td=0):
    return {"season": season, "week": week, "rush_yd": rush_yd, "rush_td": rush_td}


class TestAsOfDiscipline(unittest.TestCase):
    def test_only_uses_games_strictly_before_target_week(self):
        game_log = [game(2026, 1, 100), game(2026, 2, 100), game(2026, 3, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=3, window=4)
        # week 3's own game must NOT leak into its own projection
        self.assertEqual(result["games_used"], 2)

    def test_prior_season_games_do_not_leak_into_week_1(self):
        game_log = [game(2025, 17, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=1, window=4)
        self.assertEqual(result["games_used"], 0)
        self.assertEqual(result["confidence"], "no_data")
        self.assertEqual(result["projected_points"], 0.0)


class TestByeWeekHandling(unittest.TestCase):
    def test_bye_week_gap_does_not_count_as_a_zero_game(self):
        """Weeks 1,2,4,5 played (week 3 is a bye -- simply absent from the
        log, not a zero-stat entry). Projecting week 6 with window=4 must
        use exactly those 4 real games, not be diluted by a phantom zero
        for the bye."""
        game_log = [
            game(2026, 1, 50),
            game(2026, 2, 50),
            game(2026, 4, 50),
            game(2026, 5, 50),
        ]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=6, window=4)
        self.assertEqual(result["games_used"], 4)
        self.assertAlmostEqual(result["rolling_avg"], 5.0)  # 50 rush_yd * 0.1 = 5.0 every game


class TestRollingWindow(unittest.TestCase):
    def test_takes_only_trailing_n_games(self):
        game_log = [game(2026, w, 10 * w) for w in range(1, 8)]  # weeks 1-7, escalating yardage
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=8, window=4, decay=1.0
        )
        # trailing 4 of weeks 1-7 = weeks 4,5,6,7 -> yards 40,50,60,70 -> avg 55 -> *0.1 = 5.5
        # decay pinned to 1.0 so this tests the WINDOW, not the default decay
        # (which became 0.9 on 2026-09-01)
        self.assertEqual(result["games_used"], 4)
        self.assertAlmostEqual(result["rolling_avg"], 5.5)

    def test_unweighted_mean_not_recency_weighted(self):
        game_log = [game(2026, 1, 0, rush_td=0), game(2026, 2, 0, rush_td=2)]  # 0 pts, 12 pts
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=3, window=4, decay=1.0
        )
        self.assertAlmostEqual(result["rolling_avg"], 6.0)  # simple mean, not weighted toward week 2


class TestRecencyDecay(unittest.TestCase):
    """decay=1.0 must reproduce the plain unweighted mean exactly; decay<1.0
    shifts the average toward the most recent game. The DEFAULT became 0.9 on
    2026-09-01 (it beat 1.0 at every window on MAE, RMSE, ranking accuracy and
    Spearman rho simultaneously -- see projections.DEFAULT_WINDOW's note), so
    these tests pin decay explicitly rather than relying on it."""

    def test_decay_one_is_identical_to_unweighted_mean(self):
        game_log = [game(2026, w, 10 * w) for w in range(1, 5)]
        decayed = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4, decay=1.0
        )
        # weeks 1-4 -> 10/20/30/40 yards -> 1/2/3/4 pts -> unweighted mean 2.5
        self.assertAlmostEqual(decayed["rolling_avg"], 2.5)

    def test_default_decay_leans_on_recent_games(self):
        game_log = [game(2026, w, 10 * w) for w in range(1, 5)]
        default = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4)
        flat = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4, decay=1.0
        )
        self.assertGreater(default["rolling_avg"], flat["rolling_avg"])

    def test_decay_below_one_weights_recent_games_more(self):
        # week 1 -> 0 pts, week 2 -> 10 pts (10 rush_yd * 0.1)
        game_log = [game(2026, 1, 0), game(2026, 2, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=3, window=4, decay=0.5)
        # weights: week1 (oldest) = 0.5, week2 (most recent) = 1.0
        # weighted avg = (0.5*0 + 1.0*10) / 1.5 = 6.6667, pulled toward the
        # recent game -- above the unweighted mean of 5.0
        self.assertAlmostEqual(result["rolling_avg"], 10 / 1.5, places=2)  # rolling_avg is rounded to 2dp
        self.assertGreater(result["rolling_avg"], 5.0)

    def test_decay_field_present_in_output(self):
        game_log = [game(2026, 1, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4, decay=0.8)
        self.assertEqual(result["decay"], 0.8)

    def test_decay_out_of_range_raises(self):
        game_log = [game(2026, 1, 100)]
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, decay=0.0)
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, decay=1.1)
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, decay=-0.5)


class TestColdStart(unittest.TestCase):
    def test_rookie_first_game_flagged_low_confidence(self):
        game_log = [game(2026, 1, 40, rush_td=1)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4)
        self.assertEqual(result["games_used"], 1)
        self.assertEqual(result["confidence"], "low")
        self.assertAlmostEqual(result["rolling_avg"], 40 * 0.1 + 6)

    def test_full_window_is_not_flagged_low(self):
        game_log = [game(2026, w, 40) for w in range(1, 5)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4)
        self.assertEqual(result["confidence"], "full")

    def test_no_games_at_all_returns_no_data_not_a_crash(self):
        result = project_player([], CONFIG, as_of_season=2026, as_of_week=1, window=4)
        self.assertEqual(result["confidence"], "no_data")
        self.assertEqual(result["games_used"], 0)
        self.assertEqual(result["projected_points"], 0.0)


class TestWindowValidation(unittest.TestCase):
    def test_zero_window_raises_instead_of_returning_all_games(self):
        """Regression: eligible[-0:] is the whole list in Python, not an
        empty slice -- window=0 used to silently mean 'no limit'."""
        game_log = [game(2026, w, 10) for w in range(1, 4)]
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=4, window=0)

    def test_negative_window_raises(self):
        game_log = [game(2026, 1, 10)]
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=-1)


class TestSourceLabel(unittest.TestCase):
    def test_output_is_labeled_as_in_house_estimate(self):
        game_log = [game(2026, 1, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4)
        self.assertEqual(result["source"], "in_house_estimate")


class TestOpponentMultiplier(unittest.TestCase):
    def test_defaults_to_one_when_omitted(self):
        game_log = [game(2026, 1, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4)
        self.assertEqual(result["opponent_multiplier"], 1.0)
        self.assertAlmostEqual(result["projected_points"], result["rolling_avg"])

    def test_applied_when_provided(self):
        game_log = [game(2026, 1, 100)]  # 10.0 rolling avg
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4, opponent_multiplier=1.2
        )
        self.assertAlmostEqual(result["projected_points"], 12.0)


class TestBatchWrapper(unittest.TestCase):
    def test_project_players_keys_match_input_and_apply_per_player_multiplier(self):
        logs = {"p1": [game(2026, 1, 100)], "p2": [game(2026, 1, 50)]}
        results = project_players(
            logs, CONFIG, as_of_season=2026, as_of_week=2, opponent_multipliers={"p1": 1.5}
        )
        self.assertEqual(set(results.keys()), {"p1", "p2"})
        self.assertAlmostEqual(results["p1"]["projected_points"], 15.0)  # 10.0 * 1.5
        self.assertAlmostEqual(results["p2"]["opponent_multiplier"], 1.0)  # untouched default

    def test_project_players_applies_per_player_positional_baseline(self):
        logs = {"p1": [], "p2": [game(2026, 1, 100)]}  # p1 no_data, p2 low (1 game, window=4)
        results = project_players(
            logs, CONFIG, as_of_season=2026, as_of_week=2, window=4,
            positional_baselines={"p1": 8.0}, shrinkage_strength=1.0,
        )
        self.assertAlmostEqual(results["p1"]["projected_points"], 8.0)  # pure baseline
        self.assertIsNone(results["p2"]["positional_baseline"])  # untouched default (missing from dict)
        self.assertAlmostEqual(results["p2"]["projected_points"], results["p2"]["rolling_avg"])


class TestLegacyWindowShrinkage(unittest.TestCase):
    """The pre-2026-09-01 shrinkage rule, now reachable only via
    shrinkage_mode="window". Kept covered because it is still the reference
    the empirical-Bayes default is compared against, and because its defining
    behaviour -- no shrinkage at all once games_used >= window -- is exactly
    the defect the audit found, so a test that pins it is documentation."""

    def test_baseline_omitted_leaves_output_unchanged(self):
        game_log = [game(2026, 1, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4)
        self.assertIsNone(result["positional_baseline"])
        self.assertEqual(result["shrinkage_weight"], 1.0)
        self.assertAlmostEqual(result["shrunk_avg"], result["rolling_avg"])
        self.assertAlmostEqual(result["projected_points"], result["rolling_avg"])

    def test_no_data_with_baseline_returns_pure_baseline(self):
        # Round 3's finding, encoded as a regression test: a player with
        # zero games logged used to project a literal 0.0. With a baseline
        # supplied, games_used==0 takes it in full regardless of strength.
        result = project_player([], CONFIG, as_of_season=2026, as_of_week=1, window=4, positional_baseline=7.5)
        self.assertEqual(result["games_used"], 0)
        self.assertEqual(result["shrinkage_weight"], 0.0)
        self.assertAlmostEqual(result["projected_points"], 7.5)

    def test_no_data_with_baseline_and_opponent_multiplier_compose(self):
        result = project_player(
            [], CONFIG, as_of_season=2026, as_of_week=1, window=4,
            positional_baseline=7.5, opponent_multiplier=1.2,
        )
        self.assertAlmostEqual(result["projected_points"], 9.0)  # 7.5 * 1.2

    def test_strength_zero_is_a_no_op_for_partial_window(self):
        game_log = [game(2026, 1, 100)]  # 1 game, window=4 -> "low" tier
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4,
            positional_baseline=999.0, shrinkage_strength=0.0, shrinkage_mode="window",
        )
        self.assertAlmostEqual(result["shrunk_avg"], result["rolling_avg"])
        self.assertAlmostEqual(result["projected_points"], result["rolling_avg"])

    def test_strength_one_partial_window_is_hand_computed_blend(self):
        game_log = [game(2026, 1, 100)]  # 1 game -> rolling_avg = 10.0
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4,
            positional_baseline=6.0, shrinkage_strength=1.0, shrinkage_mode="window",
        )
        # games_used=1, window=4 -> weight = 1 - 1.0*(1 - 1/4) = 0.25
        # shrunk_avg = 0.25*10.0 + 0.75*6.0 = 7.0
        self.assertAlmostEqual(result["shrinkage_weight"], 0.25)
        self.assertAlmostEqual(result["shrunk_avg"], 7.0)
        self.assertAlmostEqual(result["projected_points"], 7.0)

    def test_full_window_is_never_shrunk_regardless_of_strength(self):
        game_log = [game(2026, w, 40) for w in range(1, 5)]  # 4 games = full window
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4,
            positional_baseline=0.0, shrinkage_strength=1.0, shrinkage_mode="window",
        )
        self.assertEqual(result["confidence"], "full")
        self.assertEqual(result["shrinkage_weight"], 1.0)
        self.assertAlmostEqual(result["shrunk_avg"], result["rolling_avg"])

    def test_shrinkage_strength_out_of_range_raises(self):
        game_log = [game(2026, 1, 100)]
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, shrinkage_strength=1.5)
        with self.assertRaises(ValueError):
            project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, shrinkage_strength=-0.1)


class TestUsageMultiplier(unittest.TestCase):
    """Round 4, idea 3 (docs/research/projection-model-backtest-findings.md):
    usage.py's trend-based multiplier, injected the same way
    opponent_multiplier is."""

    def test_defaults_to_one_when_omitted(self):
        game_log = [game(2026, 1, 100)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4)
        self.assertEqual(result["usage_multiplier"], 1.0)
        self.assertAlmostEqual(result["projected_points"], result["rolling_avg"])

    def test_applied_when_provided(self):
        game_log = [game(2026, 1, 100)]  # 10.0 rolling avg
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4, usage_multiplier=1.2,
        )
        self.assertAlmostEqual(result["projected_points"], 12.0)

    def test_composes_multiplicatively_with_opponent_multiplier(self):
        game_log = [game(2026, 1, 100)]  # 10.0 rolling avg
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4,
            opponent_multiplier=1.1, usage_multiplier=1.2,
        )
        self.assertAlmostEqual(result["projected_points"], 10.0 * 1.1 * 1.2)

    def test_composes_with_shrinkage_in_documented_order(self):
        # shrinkage acts on the average first, multipliers apply after --
        # games_used=1, window=4, strength=1.0 -> weight=0.25 (see
        # TestLegacyWindowShrinkage.test_strength_one_partial_window_is_hand_computed_blend)
        game_log = [game(2026, 1, 100)]  # rolling_avg = 10.0
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2, window=4,
            positional_baseline=6.0, shrinkage_strength=1.0, usage_multiplier=1.5,
            shrinkage_mode="window",
        )
        self.assertAlmostEqual(result["shrunk_avg"], 7.0)  # 0.25*10.0 + 0.75*6.0
        self.assertAlmostEqual(result["projected_points"], 7.0 * 1.5)

    def test_batch_wrapper_applies_per_player_usage_multiplier(self):
        logs = {"p1": [game(2026, 1, 100)], "p2": [game(2026, 1, 50)]}
        results = project_players(
            logs, CONFIG, as_of_season=2026, as_of_week=2, usage_multipliers={"p1": 1.5}
        )
        self.assertAlmostEqual(results["p1"]["projected_points"], 15.0)  # 10.0 * 1.5
        self.assertAlmostEqual(results["p2"]["usage_multiplier"], 1.0)  # untouched default


class TestEmpiricalBayesShrinkage(unittest.TestCase):
    """The default shrinkage mode as of 2026-09-01. Unlike the legacy window
    rule it applies at EVERY sample size, which is the correction for the
    measured over-dispersion (calibration slopes 0.735-0.878, all p < 1e-11)."""

    def test_full_window_is_still_shrunk(self):
        # The defining difference from the legacy mode, and the whole point:
        # a full-window sample mean is not worth its face value.
        game_log = [game(2026, w, 100) for w in range(1, 9)]  # 8 games, rolling_avg 10.0
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=9, window=8,
            positional_baseline=5.0, shrinkage_k=2.0,
        )
        self.assertEqual(result["confidence"], "full")
        self.assertLess(result["shrinkage_weight"], 1.0)
        self.assertLess(result["shrunk_avg"], result["rolling_avg"])

    def test_weight_matches_the_closed_form(self):
        game_log = [game(2026, w, 100) for w in range(1, 7)]  # 6 games
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=7, window=8,
            positional_baseline=0.0, shrinkage_k=2.0,
        )
        self.assertAlmostEqual(result["shrinkage_weight"], 6 / 8, places=4)

    def test_more_games_earns_more_of_its_own_average(self):
        thin = project_player(
            [game(2026, 1, 100)], CONFIG, as_of_season=2026, as_of_week=2,
            positional_baseline=0.0, shrinkage_k=2.0,
        )
        thick = project_player(
            [game(2026, w, 100) for w in range(1, 9)], CONFIG,
            as_of_season=2026, as_of_week=9, positional_baseline=0.0, shrinkage_k=2.0,
        )
        self.assertGreater(thick["shrinkage_weight"], thin["shrinkage_weight"])

    def test_no_baseline_means_no_shrinkage_at_all(self):
        # Same "off means arithmetically identical" contract every other
        # optional parameter keeps -- there is nothing to shrink toward.
        game_log = [game(2026, w, 100) for w in range(1, 5)]
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=5)
        self.assertEqual(result["shrinkage_weight"], 1.0)
        self.assertAlmostEqual(result["shrunk_avg"], result["rolling_avg"])

    def test_mode_none_disables_it_even_with_a_baseline(self):
        game_log = [game(2026, 1, 100)]
        result = project_player(
            game_log, CONFIG, as_of_season=2026, as_of_week=2,
            positional_baseline=0.0, shrinkage_mode="none",
        )
        self.assertEqual(result["shrinkage_weight"], 1.0)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            project_player([game(2026, 1, 100)], CONFIG, as_of_season=2026,
                           as_of_week=2, shrinkage_mode="nonsense")

    def test_mode_is_echoed_in_the_output(self):
        result = project_player([game(2026, 1, 100)], CONFIG, as_of_season=2026, as_of_week=2)
        self.assertEqual(result["shrinkage_mode"], "empirical_bayes")


class TestAvailability(unittest.TestCase):
    """projected_points is an EXPECTED value once a play probability is
    supplied; conditional_points keeps the if-he-plays number visible."""

    def _log(self):
        return [game(2026, w, 100) for w in range(1, 5)]  # rolling_avg 10.0

    def test_omitted_probability_leaves_the_number_unchanged(self):
        result = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5)
        self.assertIsNone(result["play_probability"])
        self.assertAlmostEqual(result["projected_points"], result["conditional_points"])

    def test_probability_scales_the_expected_value(self):
        result = project_player(
            self._log(), CONFIG, as_of_season=2026, as_of_week=5, play_probability=0.5
        )
        self.assertAlmostEqual(result["projected_points"], result["conditional_points"] * 0.5)

    def test_a_ruled_out_player_is_worth_essentially_nothing(self):
        result = project_player(
            self._log(), CONFIG, as_of_season=2026, as_of_week=5, play_probability=0.0
        )
        self.assertEqual(result["projected_points"], 0.0)
        self.assertGreater(result["conditional_points"], 5.0)

    def test_probability_is_clamped(self):
        high = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5,
                              play_probability=2.0)
        self.assertAlmostEqual(high["projected_points"], high["conditional_points"])
        low = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5,
                             play_probability=-1.0)
        self.assertEqual(low["projected_points"], 0.0)


class TestAffineAndInterval(unittest.TestCase):
    def _log(self):
        return [game(2026, w, 100) for w in range(1, 5)]

    def test_affine_is_applied_to_the_conditional_number(self):
        plain = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5)
        shifted = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5,
                                 affine=(1.0, 0.5))
        self.assertAlmostEqual(shifted["conditional_points"],
                               1.0 + 0.5 * plain["conditional_points"], places=2)

    def test_affine_composes_before_availability(self):
        result = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5,
                                affine=(0.0, 0.5), play_probability=0.5)
        self.assertAlmostEqual(result["projected_points"],
                               result["conditional_points"] * 0.5, places=2)

    def test_interval_is_absent_unless_supplied(self):
        result = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5)
        self.assertIsNone(result["floor"])
        self.assertIsNone(result["ceiling"])

    def test_interval_is_echoed_and_scaled_by_availability(self):
        plain = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5,
                               interval=(4.0, 20.0))
        self.assertAlmostEqual(plain["floor"], 4.0)
        self.assertAlmostEqual(plain["ceiling"], 20.0)
        scaled = project_player(self._log(), CONFIG, as_of_season=2026, as_of_week=5,
                                interval=(4.0, 20.0), play_probability=0.5)
        self.assertAlmostEqual(scaled["floor"], 2.0)
        self.assertAlmostEqual(scaled["ceiling"], 10.0)


class TestBatchThreading(unittest.TestCase):
    """Every new per-player parameter has to actually reach project_player --
    a silently dropped one would make a whole evaluation meaningless without
    failing anything."""

    def test_batch_threads_availability_and_k_and_affine(self):
        logs = {"p1": [game(2026, w, 100) for w in range(1, 5)],
                "p2": [game(2026, w, 100) for w in range(1, 5)]}
        results = project_players(
            logs, CONFIG, as_of_season=2026, as_of_week=5,
            play_probabilities={"p1": 0.5},
            shrinkage_ks={"p1": 3.0},
            affines={"p1": (0.0, 0.5)},
            intervals={"p1": (1.0, 9.0)},
        )
        self.assertAlmostEqual(results["p1"]["play_probability"], 0.5)
        self.assertEqual(results["p1"]["shrinkage_k"], 3.0)
        self.assertAlmostEqual(results["p1"]["floor"], 0.5)
        # p2 is untouched by any of them
        self.assertIsNone(results["p2"]["play_probability"])
        self.assertIsNone(results["p2"]["floor"])
        self.assertGreater(results["p2"]["projected_points"], results["p1"]["projected_points"])


if __name__ == "__main__":
    unittest.main()
