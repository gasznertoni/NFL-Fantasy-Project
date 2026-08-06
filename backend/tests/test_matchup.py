"""Unit tests for matchup.py: record aggregation, tier boundaries (the
overlapping-edge convention documented in matchup.py's module docstring),
and the last-season/this-season cutover rule."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matchup import (  # noqa: E402
    compute_opponent_multiplier,
    multiplier_from_win_pct,
    team_record,
    win_pct,
)


def played_game(season, week, home, away, home_score, away_score):
    return {
        "season": season, "week": week,
        "home_team": home, "away_team": away,
        "home_score": home_score, "away_score": away_score,
    }


def unplayed_game(season, week, home, away):
    return {"season": season, "week": week, "home_team": home, "away_team": away, "home_score": None, "away_score": None}


class TestWinPct(unittest.TestCase):
    def test_no_games_is_zero_not_a_crash(self):
        self.assertEqual(win_pct(0, 0), 0.0)

    def test_ties_count_as_half_win_half_loss(self):
        # 1 win, 1 loss, 1 tie -> (1 + 0.5) / 3
        self.assertAlmostEqual(win_pct(1, 1, ties=1), 0.5)


class TestTeamRecord(unittest.TestCase):
    def test_aggregates_home_and_away_correctly(self):
        games = [
            played_game(2026, 1, "KC", "BUF", 24, 20),   # KC wins at home
            played_game(2026, 2, "DEN", "KC", 17, 27),   # KC wins on the road
            played_game(2026, 3, "KC", "LAC", 14, 21),   # KC loses at home
        ]
        record = team_record(games, "KC", 2026)
        self.assertEqual(record, {"wins": 2, "losses": 1, "ties": 0, "games_played": 3, "win_pct": 2 / 3})

    def test_as_of_filter_excludes_games_at_or_after_target_week(self):
        games = [played_game(2026, w, "KC", "BUF", 24, 20) for w in range(1, 6)]
        record = team_record(games, "KC", 2026, before_week=3)
        self.assertEqual(record["games_played"], 2)  # only weeks 1, 2

    def test_unplayed_future_games_are_skipped(self):
        games = [played_game(2026, 1, "KC", "BUF", 24, 20), unplayed_game(2026, 2, "KC", "DEN")]
        record = team_record(games, "KC", 2026)
        self.assertEqual(record["games_played"], 1)

    def test_bye_week_is_just_absent_not_a_loss(self):
        # team plays weeks 1 and 3 only (bye week 2) -- no row for week 2 at all
        games = [played_game(2026, 1, "KC", "BUF", 24, 20), played_game(2026, 3, "KC", "LAC", 24, 20)]
        record = team_record(games, "KC", 2026)
        self.assertEqual(record["games_played"], 2)
        self.assertEqual(record["losses"], 0)

    def test_other_teams_games_are_ignored(self):
        games = [played_game(2026, 1, "BUF", "MIA", 24, 20)]
        record = team_record(games, "KC", 2026)
        self.assertEqual(record["games_played"], 0)


class TestMultiplierTierBoundaries(unittest.TestCase):
    def test_below_040_gets_boosted(self):
        self.assertEqual(multiplier_from_win_pct(0.399), 1.08)

    def test_exactly_040_is_in_the_average_tier_not_the_boosted_one(self):
        self.assertEqual(multiplier_from_win_pct(0.400), 1.00)

    def test_exactly_060_is_in_the_098_tier_not_the_100_tier(self):
        self.assertEqual(multiplier_from_win_pct(0.600), 0.98)

    def test_exactly_075_is_in_the_095_tier_not_the_098_tier(self):
        self.assertEqual(multiplier_from_win_pct(0.750), 0.95)

    def test_above_075_stays_in_the_top_suppressed_tier(self):
        self.assertEqual(multiplier_from_win_pct(1.0), 0.95)

    def test_zero_record_gets_boosted(self):
        self.assertEqual(multiplier_from_win_pct(0.0), 1.08)


class TestComputeOpponentMultiplier(unittest.TestCase):
    def test_uses_prior_season_when_fewer_than_4_games_played_this_season(self):
        games = [
            # 2025: KC went 3-1 (0.75 win pct -> 0.95 multiplier)
            played_game(2025, 1, "KC", "BUF", 24, 20),
            played_game(2025, 2, "KC", "DEN", 24, 20),
            played_game(2025, 3, "KC", "LAC", 24, 20),
            played_game(2025, 4, "KC", "LV", 10, 20),
            # 2026: only 2 games played so far
            played_game(2026, 1, "KC", "BUF", 24, 20),
            played_game(2026, 2, "KC", "DEN", 24, 20),
        ]
        result = compute_opponent_multiplier(games, "KC", target_season=2026, target_week=3)
        self.assertEqual(result["source"], "prior_season")
        self.assertAlmostEqual(result["win_pct"], 0.75)
        self.assertEqual(result["multiplier"], 0.95)
        self.assertEqual(result["games_played_this_season"], 2)

    def test_switches_to_current_season_once_4_games_played(self):
        games = [
            played_game(2025, 1, "KC", "BUF", 24, 20),  # 2025: 1-0, would be a different tier
            *[played_game(2026, w, "KC", "BUF", 10, 20) for w in range(1, 5)],  # 2026: 0-4 so far
        ]
        result = compute_opponent_multiplier(games, "KC", target_season=2026, target_week=5)
        self.assertEqual(result["source"], "current_season")
        self.assertAlmostEqual(result["win_pct"], 0.0)
        self.assertEqual(result["multiplier"], 1.08)

    def test_no_data_in_either_season_returns_neutral_multiplier(self):
        result = compute_opponent_multiplier([], "KC", target_season=2026, target_week=1)
        self.assertEqual(result["source"], "no_data")
        self.assertEqual(result["multiplier"], 1.0)

    def test_custom_tier_list_is_respected(self):
        games = [played_game(2025, w, "KC", "BUF", 24, 20) for w in range(1, 5)]  # 2025: 4-0
        custom_tiers = [{"below": None, "multiplier": 0.5}]  # everything maps to 0.5
        result = compute_opponent_multiplier(games, "KC", target_season=2026, target_week=1, tiers=custom_tiers)
        self.assertEqual(result["multiplier"], 0.5)


if __name__ == "__main__":
    unittest.main()
