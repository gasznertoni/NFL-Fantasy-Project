"""Unit tests for generate_report.py's pure assembly half (everything
except the `load_*` network adapters, per that module's own docstring --
those need nflreadpy/network and are exercised by running the script
directly, not by this suite)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_report import (  # noqa: E402
    assemble_player_pool,
    assemble_weekly_report,
    build_player_pool_entry,
    build_projection_entry,
    build_waiver_target_entry,
    build_weekly_report_and_pool,
    normalize_team,
    opponent_for_team_week,
)

CONFIG = {"linear": {"rush_yd": 0.1, "rush_td": 6}}
HEALTHY = {"designation": "Healthy", "riskLevel": "none", "summary": None}


class TestNormalizeTeam(unittest.TestCase):
    def test_maps_la_to_lar(self):
        self.assertEqual(normalize_team("LA"), "LAR")

    def test_passes_through_unmapped_teams(self):
        self.assertEqual(normalize_team("BUF"), "BUF")

    def test_none_stays_none(self):
        self.assertIsNone(normalize_team(None))


class TestOpponentForTeamWeek(unittest.TestCase):
    SCHEDULE = [
        {"season": 2026, "week": 1, "home_team": "BUF", "away_team": "MIA"},
        {"season": 2026, "week": 2, "home_team": "NYJ", "away_team": "NE"},
    ]

    def test_finds_opponent_as_home_team(self):
        self.assertEqual(opponent_for_team_week(self.SCHEDULE, "BUF", 2026, 1), "MIA")

    def test_finds_opponent_as_away_team(self):
        self.assertEqual(opponent_for_team_week(self.SCHEDULE, "MIA", 2026, 1), "BUF")

    def test_bye_week_returns_none(self):
        self.assertIsNone(opponent_for_team_week(self.SCHEDULE, "BUF", 2026, 2))

    def test_wrong_season_returns_none(self):
        self.assertIsNone(opponent_for_team_week(self.SCHEDULE, "BUF", 2025, 1))


class TestAssemblyShapes(unittest.TestCase):
    def test_build_player_pool_entry_shape(self):
        player = {"playerId": "00-1234567", "name": "Test Player", "position": "WR", "team": "BUF"}
        entry = build_player_pool_entry(player, dict(HEALTHY))
        self.assertEqual(
            entry,
            {"playerId": "00-1234567", "name": "Test Player", "position": "WR", "team": "BUF", "newsFlag": HEALTHY},
        )

    def test_build_projection_entry_nests_projection_object(self):
        candidate = {
            "playerId": "00-1234567",
            "name": "Test Player",
            "position": "RB",
            "team": "BUF",
            "opponent": "MIA",
            "points": 12.4,
            "news_flag": dict(HEALTHY),
        }
        entry = build_projection_entry(candidate)
        self.assertEqual(entry["projection"], {"tier": "in_house_estimate", "points": 12.4, "tierLabel": "Our estimate", "source": "In-house model"})
        self.assertEqual(entry["opponent"], "MIA")
        self.assertEqual(entry["newsFlag"], HEALTHY)

    def test_build_waiver_target_entry_adds_rationale(self):
        candidate = {
            "playerId": "00-1234567",
            "name": "Test Player",
            "position": "RB",
            "team": "BUF",
            "opponent": "MIA",
            "points": 8.0,
            "news_flag": dict(HEALTHY),
        }
        entry = build_waiver_target_entry(candidate, "a good pickup")
        self.assertEqual(entry["rationale"], "a good pickup")

    def test_assemble_weekly_report_top_level_keys(self):
        report = assemble_weekly_report(1, "2026-09-04T13:00:00Z", [], [])
        self.assertEqual(
            set(report.keys()), {"week", "leagueFormatAssumption", "generatedAt", "projections", "waiverTargets"}
        )
        self.assertEqual(report["leagueFormatAssumption"], "half_ppr")

    def test_assemble_player_pool_shape(self):
        self.assertEqual(assemble_player_pool([{"playerId": "x"}]), {"players": [{"playerId": "x"}]})


class TestBuildWeeklyReportAndPool(unittest.TestCase):
    def setUp(self):
        self.pool = [
            {"playerId": "qb1", "name": "Starting QB", "position": "QB", "team": "BUF"},
            {"playerId": "rb1", "name": "Bye Week RB", "position": "RB", "team": "NYJ"},
        ]
        self.schedule = [{"season": 2026, "week": 1, "home_team": "BUF", "away_team": "MIA"}]
        self.game_logs = {"qb1": [{"season": 2026, "week": w, "rush_yd": 50} for w in range(1, 1)]}
        self.news_flags = {"qb1": dict(HEALTHY)}

    def test_players_with_no_scheduled_game_are_excluded_from_the_report_but_kept_in_pool(self):
        weekly_report, player_pool = build_weekly_report_and_pool(
            2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, self.news_flags
        )
        report_ids = {p["playerId"] for p in weekly_report["projections"]}
        pool_ids = {p["playerId"] for p in player_pool["players"]}
        self.assertIn("qb1", report_ids)
        self.assertNotIn("rb1", report_ids)  # NYJ has no game in the schedule fixture -> bye
        self.assertEqual(pool_ids, {"qb1", "rb1"})

    def test_missing_news_flag_defaults_to_healthy(self):
        weekly_report, player_pool = build_weekly_report_and_pool(
            2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, {}
        )
        qb_entry = next(p for p in weekly_report["projections"] if p["playerId"] == "qb1")
        self.assertEqual(qb_entry["newsFlag"]["designation"], "Healthy")

    def test_generated_at_defaults_to_something_non_empty(self):
        weekly_report, _ = build_weekly_report_and_pool(
            2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, self.news_flags
        )
        self.assertTrue(weekly_report["generatedAt"])

    def test_waiver_targets_are_selected_from_the_same_weeks_candidates(self):
        pool = [{"playerId": f"wr{i}", "name": f"WR {i}", "position": "WR", "team": "BUF"} for i in range(5)]
        schedule = [{"season": 2026, "week": 1, "home_team": "BUF", "away_team": "MIA"}]
        game_logs = {
            f"wr{i}": [{"season": 2025, "week": 17, "rush_yd": 10 * i}] for i in range(5)
        }  # prior-season only -- all cold-start for week 1, differentiate by index for ranking sanity
        weekly_report, _ = build_weekly_report_and_pool(2026, 1, CONFIG, pool, schedule, game_logs, {})
        self.assertLessEqual(len(weekly_report["waiverTargets"]), 3)
        for target in weekly_report["waiverTargets"]:
            self.assertIn("rationale", target)


if __name__ == "__main__":
    unittest.main()
