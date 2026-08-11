"""Unit tests for baseline.py: as-of correctness, the "thin" vs "all"
population distinction (module docstring's core claim -- they must
actually differ, not just be two code paths that behave identically), the
mean/median statistic, the prior-season week-1 fallback, and the
single-week vs batch-across-weeks agreement (positional_baselines_by_week's
incremental accumulator must produce identical results to calling
positional_baselines once per week)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baseline import (  # noqa: E402
    baselines_by_player_week,
    baselines_by_player_week_for_shrinkage,
    per_game_points_by_position,
    positional_baselines,
    positional_baselines_by_week,
)

CONFIG = {"linear": {"pass_yd": 0.04}}  # 25 yards = 1.0 point, keeps numbers simple


def game(season, week, position, pass_yd):
    return {"season": season, "week": week, "position": position, "pass_yd": pass_yd}


class TestPerGamePointsByPosition(unittest.TestCase):
    def test_as_of_excludes_current_and_future_weeks(self):
        log = {"p1": [game(2025, 1, "RB", 250), game(2025, 3, "RB", 500)]}
        pts = per_game_points_by_position(log, CONFIG, season=2025, before_week=3, population="all")
        self.assertEqual(pts["RB"], [10.0])  # only week 1 (< 3); week 3 itself excluded

    def test_other_seasons_excluded(self):
        log = {"p1": [game(2024, 1, "RB", 250), game(2025, 1, "RB", 500)]}
        pts = per_game_points_by_position(log, CONFIG, season=2025, before_week=2, population="all")
        self.assertEqual(pts["RB"], [20.0])

    def test_before_week_none_means_whole_season(self):
        log = {"p1": [game(2025, w, "RB", 250) for w in range(1, 5)]}
        pts = per_game_points_by_position(log, CONFIG, season=2025, before_week=None, population="all")
        self.assertEqual(len(pts["RB"]), 4)

    def test_thin_population_excludes_games_past_the_window(self):
        # 8 games, window=6 -> only the first 6 (by week order) count as "thin"
        log = {"p1": [game(2025, w, "RB", 250) for w in range(1, 9)]}
        thin = per_game_points_by_position(log, CONFIG, season=2025, before_week=None, window=6, population="thin")
        all_pop = per_game_points_by_position(log, CONFIG, season=2025, before_week=None, window=6, population="all")
        self.assertEqual(len(thin["RB"]), 6)
        self.assertEqual(len(all_pop["RB"]), 8)

    def test_thin_index_is_per_player_not_global(self):
        # Two players, each with only 2 games -- both fully within a window=6
        # "thin" cutoff, so thin and all must agree here (regression guard
        # against accidentally sharing one running index across players).
        log = {
            "p1": [game(2025, 1, "RB", 250), game(2025, 2, "RB", 250)],
            "p2": [game(2025, 1, "RB", 250), game(2025, 2, "RB", 250)],
        }
        thin = per_game_points_by_position(log, CONFIG, season=2025, before_week=None, window=6, population="thin")
        self.assertEqual(len(thin["RB"]), 4)

    def test_position_missing_from_log_entries_is_skipped(self):
        log = {"p1": [{"season": 2025, "week": 1, "pass_yd": 250}]}  # no "position" key
        pts = per_game_points_by_position(log, CONFIG, season=2025, before_week=2, population="all")
        self.assertEqual(pts, {})

    def test_invalid_population_raises(self):
        with self.assertRaises(ValueError):
            per_game_points_by_position({}, CONFIG, season=2025, before_week=2, population="bogus")

    def test_debut_population_is_first_game_only(self):
        log = {
            "p1": [game(2025, 1, "RB", 250), game(2025, 2, "RB", 500)],  # debut = week 1 (10 pts)
            "p2": [game(2025, 3, "RB", 1000)],  # debut = week 3 (40 pts)
        }
        debut = per_game_points_by_position(log, CONFIG, season=2025, before_week=5, population="debut")
        self.assertEqual(sorted(debut["RB"]), [10.0, 40.0])

    def test_min_week_excludes_early_games_regardless_of_population(self):
        # p1's debut (week 1, 10 pts) must be excluded by min_week=2 even
        # though it's exactly what population="debut" would otherwise keep.
        log = {
            "p1": [game(2025, 1, "RB", 250)],
            "p2": [game(2025, 3, "RB", 1000)],
        }
        debut_all = per_game_points_by_position(log, CONFIG, season=2025, before_week=5, population="debut")
        debut_min_week = per_game_points_by_position(
            log, CONFIG, season=2025, before_week=5, population="debut", min_week=2,
        )
        self.assertEqual(sorted(debut_all["RB"]), [10.0, 40.0])
        self.assertEqual(debut_min_week["RB"], [40.0])

    def test_min_week_does_not_shift_the_games_played_before_index(self):
        # p1's week-2 game is their SECOND game (games_played_before=1,
        # "thin" not "debut") even though min_week=2 excludes their week-1
        # game from the output -- min_week must not renumber the index.
        log = {"p1": [game(2025, 1, "RB", 250), game(2025, 2, "RB", 500)]}
        debut_min_week = per_game_points_by_position(
            log, CONFIG, season=2025, before_week=5, population="debut", min_week=2,
        )
        self.assertEqual(debut_min_week, {})  # the only debut game (week 1) was excluded by min_week


class TestPositionalBaselines(unittest.TestCase):
    def test_mean_statistic(self):
        log = {"p1": [game(2025, 1, "RB", 250)], "p2": [game(2025, 1, "RB", 500)]}  # 10, 20
        out = positional_baselines(log, CONFIG, season=2025, as_of_week=2, statistic="mean")
        self.assertEqual(out["RB"]["baseline"], 15.0)
        self.assertEqual(out["RB"]["n"], 2)
        self.assertEqual(out["RB"]["source"], "current_season")

    def test_median_statistic_differs_from_mean_on_skewed_data(self):
        log = {
            "p1": [game(2025, 1, "RB", 25)],   # 1 pt
            "p2": [game(2025, 1, "RB", 25)],   # 1 pt
            "p3": [game(2025, 1, "RB", 2500)],  # 100 pt outlier
        }
        mean_out = positional_baselines(log, CONFIG, season=2025, as_of_week=2, statistic="mean")
        median_out = positional_baselines(log, CONFIG, season=2025, as_of_week=2, statistic="median")
        self.assertAlmostEqual(mean_out["RB"]["baseline"], 34.0)
        self.assertEqual(median_out["RB"]["baseline"], 1.0)

    def test_no_current_or_prior_season_data_is_no_data(self):
        log = {"p1": [game(2025, 1, "RB", 250)]}
        out = positional_baselines(log, CONFIG, season=2025, as_of_week=1, statistic="mean")
        self.assertEqual(out["RB"], {"baseline": None, "n": 0, "source": "no_data"})

    def test_week_one_falls_back_to_prior_season(self):
        log = {"p1": [game(2025, w, "RB", 250) for w in range(1, 4)]}  # full 2025 season
        out = positional_baselines(log, CONFIG, season=2026, as_of_week=1, statistic="mean")
        self.assertEqual(out["RB"]["source"], "prior_season")
        self.assertEqual(out["RB"]["n"], 3)

    def test_current_season_data_preferred_over_prior_season(self):
        log = {
            "p1": [game(2025, w, "RB", 250) for w in range(1, 4)],  # prior season, would fall back
            "p2": [game(2026, 1, "RB", 1000)],  # current season, week 1 already played
        }
        out = positional_baselines(log, CONFIG, season=2026, as_of_week=2, statistic="mean")
        self.assertEqual(out["RB"]["source"], "current_season")
        self.assertEqual(out["RB"]["n"], 1)

    def test_invalid_statistic_raises(self):
        with self.assertRaises(ValueError):
            positional_baselines({}, CONFIG, season=2025, as_of_week=2, statistic="bogus")


class TestPositionalBaselinesByWeek(unittest.TestCase):
    def _pool(self):
        return {
            "p1": [game(2025, w, "RB", pts) for w, pts in zip([1, 2, 3], [250, 500, 750])],
            "p2": [game(2025, w, "RB", 2500) for w in range(1, 9)],
            "p3": [game(2025, w, "WR", 400) for w in range(1, 4)],
        }

    def test_matches_single_week_calls(self):
        pool = self._pool()
        weeks = [1, 2, 3, 5, 9]
        batch = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=weeks, window=6, population="thin")
        for week in weeks:
            single = positional_baselines(pool, CONFIG, season=2025, as_of_week=week, window=6, population="thin")
            self.assertEqual(batch[week], single, f"mismatch at week {week}")

    def test_matches_single_week_calls_for_all_population(self):
        pool = self._pool()
        weeks = [1, 5, 9]
        batch = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=weeks, window=6, population="all")
        for week in weeks:
            single = positional_baselines(pool, CONFIG, season=2025, as_of_week=week, window=6, population="all")
            self.assertEqual(batch[week], single, f"mismatch at week {week}")

    def test_unsorted_input_weeks_still_correct(self):
        pool = self._pool()
        batch = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9, 1, 5], window=6, population="thin")
        single_week_5 = positional_baselines(pool, CONFIG, season=2025, as_of_week=5, window=6, population="thin")
        self.assertEqual(batch[5], single_week_5)


class TestBaselinesByPlayerWeek(unittest.TestCase):
    def test_resolves_position_and_week_to_scalar(self):
        pool = {"p1": [game(2025, w, "RB", 250) for w in range(1, 8)]}
        by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[8], window=6, population="thin")
        resolved = baselines_by_player_week(pool, by_week)
        self.assertEqual(resolved["p1"][8], by_week[8]["RB"]["baseline"])

    def test_player_with_no_position_gets_no_entry(self):
        pool = {"p1": [{"season": 2025, "week": 1, "pass_yd": 100}]}
        by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[2], window=6)
        resolved = baselines_by_player_week(pool, by_week)
        self.assertNotIn("p1", resolved)

    def test_no_data_weeks_are_omitted_not_none(self):
        # Week 1, no prior season -> source="no_data", baseline=None -- must
        # be omitted from the per-player dict, not stored as None, so a
        # plain `.get(week)` reads the same as "no baseline" everywhere else.
        pool = {"p1": [game(2025, 1, "RB", 250)]}
        by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[1], window=6)
        resolved = baselines_by_player_week(pool, by_week)
        self.assertEqual(resolved["p1"], {})


class TestBaselinesByPlayerWeekForShrinkage(unittest.TestCase):
    """The dual-population resolver: picks debut vs thin per player-week
    based on that player's ACTUAL games_used at that week, not a single
    population applied to everyone -- see baseline.py's module docstring
    for why a single population overshoots the no_data tier."""

    def _pool(self):
        return {
            # no_data at week 9: only an actual result at week 9 itself,
            # zero games logged before it.
            "no_data_player": [game(2025, 9, "RB", 200)],
            # low tier at week 9: exactly 1 prior game (week 3).
            "low_player": [game(2025, 3, "RB", 50), game(2025, 9, "RB", 200)],
            # full tier at week 9: 6 prior games (weeks 1-6), window=6.
            "full_player": [game(2025, w, "RB", 2500) for w in range(1, 7)] + [game(2025, 9, "RB", 200)],
        }

    def test_no_data_player_gets_debut_baseline(self):
        pool = self._pool()
        debut_by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9], window=6, population="debut")
        thin_by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9], window=6, population="thin")
        resolved = baselines_by_player_week_for_shrinkage(pool, season=2025, window=6,
                                                            debut_baselines_by_week=debut_by_week,
                                                            thin_baselines_by_week=thin_by_week)
        self.assertEqual(resolved["no_data_player"][9], debut_by_week[9]["RB"]["baseline"])

    def test_low_player_gets_thin_baseline(self):
        pool = self._pool()
        debut_by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9], window=6, population="debut")
        thin_by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9], window=6, population="thin")
        resolved = baselines_by_player_week_for_shrinkage(pool, season=2025, window=6,
                                                            debut_baselines_by_week=debut_by_week,
                                                            thin_baselines_by_week=thin_by_week)
        self.assertEqual(resolved["low_player"][9], thin_by_week[9]["RB"]["baseline"])

    def test_full_player_is_omitted(self):
        pool = self._pool()
        debut_by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9], window=6, population="debut")
        thin_by_week = positional_baselines_by_week(pool, CONFIG, season=2025, weeks=[9], window=6, population="thin")
        resolved = baselines_by_player_week_for_shrinkage(pool, season=2025, window=6,
                                                            debut_baselines_by_week=debut_by_week,
                                                            thin_baselines_by_week=thin_by_week)
        self.assertEqual(resolved["full_player"], {})


if __name__ == "__main__":
    unittest.main()
