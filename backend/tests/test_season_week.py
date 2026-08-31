"""Unit tests for season_week.py's pure half -- hand-built schedule rows
with known expected output, same pattern as test_dst.py. No network (the
load_current_week_nflreadpy adapter is NOT exercised here)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from season_week import current_week_from_schedule  # noqa: E402


class TestCurrentWeekFromSchedule(unittest.TestCase):
    def test_returns_earliest_week_with_an_unplayed_game(self):
        schedule_games = [
            {"season": 2026, "week": 1, "home_score": 24, "away_score": 17},
            {"season": 2026, "week": 2, "home_score": None, "away_score": None},
            {"season": 2026, "week": 3, "home_score": None, "away_score": None},
        ]
        self.assertEqual(current_week_from_schedule(schedule_games, 2026), 2)

    def test_ignores_other_seasons(self):
        schedule_games = [
            {"season": 2025, "week": 1, "home_score": None, "away_score": None},
            {"season": 2026, "week": 1, "home_score": None, "away_score": None},
        ]
        self.assertEqual(current_week_from_schedule(schedule_games, 2026), 1)

    def test_a_week_counts_as_unplayed_if_any_game_in_it_lacks_a_score(self):
        schedule_games = [
            {"season": 2026, "week": 1, "home_score": 24, "away_score": 17},
            {"season": 2026, "week": 1, "home_score": None, "away_score": None},
        ]
        self.assertEqual(current_week_from_schedule(schedule_games, 2026), 1)

    def test_falls_back_to_last_week_when_the_full_loaded_season_is_played(self):
        schedule_games = [
            {"season": 2026, "week": 1, "home_score": 24, "away_score": 17},
            {"season": 2026, "week": 2, "home_score": 20, "away_score": 13},
        ]
        self.assertEqual(current_week_from_schedule(schedule_games, 2026), 2)

    def test_returns_none_when_no_games_loaded_for_the_season(self):
        schedule_games = [
            {"season": 2025, "week": 1, "home_score": None, "away_score": None},
        ]
        self.assertIsNone(current_week_from_schedule(schedule_games, 2026))

    def test_returns_none_for_empty_schedule(self):
        self.assertIsNone(current_week_from_schedule([], 2026))


if __name__ == "__main__":
    unittest.main()
