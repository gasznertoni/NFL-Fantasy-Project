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
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=8, window=4)
        # trailing 4 of weeks 1-7 = weeks 4,5,6,7 -> yards 40,50,60,70 -> avg 55 -> *0.1 = 5.5
        self.assertEqual(result["games_used"], 4)
        self.assertAlmostEqual(result["rolling_avg"], 5.5)

    def test_unweighted_mean_not_recency_weighted(self):
        game_log = [game(2026, 1, 0, rush_td=0), game(2026, 2, 0, rush_td=2)]  # 0 pts, 12 pts
        result = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=3, window=4)
        self.assertAlmostEqual(result["rolling_avg"], 6.0)  # simple mean, not weighted toward week 2


class TestRecencyDecay(unittest.TestCase):
    """decay=1.0 (default) must reproduce the plain unweighted mean exactly
    -- decay<1.0 shifts the average toward the most recent game, the fix
    for the bias sign-flip flagged in
    docs/research/projection-model-backtest-findings.md ("ideas for
    improving accuracy" #1)."""

    def test_decay_one_is_identical_to_unweighted_mean(self):
        game_log = [game(2026, w, 10 * w) for w in range(1, 5)]
        unweighted = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4)
        decayed = project_player(game_log, CONFIG, as_of_season=2026, as_of_week=5, window=4, decay=1.0)
        self.assertAlmostEqual(unweighted["rolling_avg"], decayed["rolling_avg"])

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


if __name__ == "__main__":
    unittest.main()
