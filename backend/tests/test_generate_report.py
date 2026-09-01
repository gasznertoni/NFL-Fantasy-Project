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
            set(report.keys()),
            {"week", "leagueId", "leagueFormatAssumption", "generatedAt", "projections", "waiverTargets"},
        )
        self.assertEqual(report["leagueFormatAssumption"], "ppr")
        self.assertEqual(report["leagueId"], "league-1")

    def test_assemble_weekly_report_league_id_and_format_kwargs(self):
        """New kwargs pass through to the returned dict; defaults preserved for
        callers that don't supply them (backward-compat contract)."""
        report = assemble_weekly_report(
            5, "2026-10-01T12:00:00Z", [], [],
            league_id="league-2", league_format="half_ppr",
        )
        self.assertEqual(report["leagueId"], "league-2")
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


class TestConsensusTierWiring(unittest.TestCase):
    """CLAUDE.md v8 Next Steps item 3: a player present in
    consensus_projections gets the FantasyPros tier instead of the
    in-house estimate; everyone else is unaffected."""

    def setUp(self):
        self.pool = [
            {"playerId": "qb1", "name": "Consensus QB", "position": "QB", "team": "BUF"},
            {"playerId": "rb1", "name": "In-House RB", "position": "RB", "team": "BUF"},
        ]
        self.schedule = [{"season": 2026, "week": 1, "home_team": "BUF", "away_team": "MIA"}]
        self.game_logs = {"rb1": [{"season": 2026, "week": w, "rush_yd": 50} for w in range(1, 1)]}

    def _consensus_projection_for(self, consensus):
        weekly_report, _ = build_weekly_report_and_pool(
            2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, {},
            consensus_projections=consensus,
        )
        return next(
            p for p in weekly_report["projections"] if p["playerId"] == "qb1"
        )["projection"]

    def test_player_in_consensus_projections_gets_the_consensus_tier(self):
        # No contributing_sources on the entry -- the tier-level label is the
        # honest fallback, because provenance genuinely is not known here.
        projection = self._consensus_projection_for(
            {"qb1": {"source": "consensus", "fpid": 1, "projected_points": 30.5}}
        )
        self.assertEqual(
            projection,
            {
                "tier": "consensus",
                "points": 30.5,
                "tierLabel": "Consensus projection",
                "source": "FantasyPros + Rotowire",
            },
        )

    def test_consensus_source_names_the_feeds_that_actually_fed_the_player(self):
        # The two consensus feeds cover overlapping but different players, so
        # the tier label names what the tier CAN use and this names what
        # actually produced THIS number. Labelling a FantasyPros-only player
        # "FantasyPros + Rotowire" would claim two sources agreed where one
        # had no data at all.
        single = self._consensus_projection_for({
            "qb1": {"source": "consensus", "projected_points": 30.5,
                    "contributing_sources": ["FantasyPros"]},
        })
        self.assertEqual(single["source"], "FantasyPros")

        other = self._consensus_projection_for({
            "qb1": {"source": "consensus", "projected_points": 30.5,
                    "contributing_sources": ["Rotowire"]},
        })
        self.assertEqual(other["source"], "Rotowire")

        blended = self._consensus_projection_for({
            "qb1": {"source": "consensus", "projected_points": 30.5,
                    "contributing_sources": ["FantasyPros", "Rotowire"]},
        })
        self.assertEqual(blended["source"], "FantasyPros + Rotowire")

    def test_player_absent_from_consensus_projections_still_gets_in_house_estimate(self):
        consensus = {"qb1": {"source": "consensus", "fpid": 1, "projected_points": 30.5}}
        weekly_report, _ = build_weekly_report_and_pool(
            2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, {}, consensus_projections=consensus
        )
        rb_entry = next(p for p in weekly_report["projections"] if p["playerId"] == "rb1")
        self.assertEqual(rb_entry["projection"]["tier"], "in_house_estimate")

    def test_no_consensus_projections_is_identical_to_the_old_all_in_house_behavior(self):
        with_none, _ = build_weekly_report_and_pool(
            2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, {}, consensus_projections=None
        )
        without_arg, _ = build_weekly_report_and_pool(2026, 1, CONFIG, self.pool, self.schedule, self.game_logs, {})
        tiers_with_none = {p["playerId"]: p["projection"]["tier"] for p in with_none["projections"]}
        tiers_without_arg = {p["playerId"]: p["projection"]["tier"] for p in without_arg["projections"]}
        self.assertEqual(tiers_with_none, tiers_without_arg)
        self.assertTrue(all(t == "in_house_estimate" for t in tiers_with_none.values()))


class TestDualLeagueScoring(unittest.TestCase):
    """Acceptance criterion: calling build_weekly_report_and_pool with two
    scoring configs that differ only in pass_td produces different QB
    projected points for the same player -- confirming that the scoring
    isolation property holds (no module-level state bleeds between calls).

    Setup: one QB with a single game log entry (week 1 of 2026) with
    pass_td=2. Projecting for week 2 with the in-house estimate tier.
    Config A: pass_td=6 → 2*6 = 12.0 pts.
    Config B: pass_td=4 → 2*4 = 8.0 pts.

    No shrinkage applies (positional_baseline is None, the default in
    build_weekly_report_and_pool → project_player), so the result is
    exactly the scoring-config-weighted average of the one game's stats.
    """

    def setUp(self):
        self.pool = [
            {"playerId": "qb-dual-test", "name": "Dual Test QB", "position": "QB", "team": "BUF"}
        ]
        # Week 2 is the target; week 1 is the only game in the log (as-of
        # discipline: games_before includes weeks < as_of_week same season).
        self.schedule = [{"season": 2026, "week": 2, "home_team": "BUF", "away_team": "MIA"}]
        self.game_logs = {
            "qb-dual-test": [{"season": 2026, "week": 1, "pass_td": 2}]
        }
        self.config_a = {"linear": {"pass_td": 6}}   # 2 TDs * 6 = 12.0
        self.config_b = {"linear": {"pass_td": 4}}   # 2 TDs * 4 = 8.0

    def _get_qb_points(self, report: dict) -> float:
        entry = next(p for p in report["projections"] if p["playerId"] == "qb-dual-test")
        return entry["projection"]["points"]

    def test_different_scoring_configs_produce_different_qb_projected_points(self):
        report_a, _ = build_weekly_report_and_pool(
            2026, 2, self.config_a, self.pool, self.schedule, self.game_logs, {}
        )
        report_b, _ = build_weekly_report_and_pool(
            2026, 2, self.config_b, self.pool, self.schedule, self.game_logs, {}
        )
        pts_a = self._get_qb_points(report_a)
        pts_b = self._get_qb_points(report_b)
        # QB calibration scale (0.85) applies: 2 TDs * 6 * 0.85 = 10.2, * 4 * 0.85 = 6.8
        from projections import POSITION_CALIBRATION_SCALE
        qb_scale = POSITION_CALIBRATION_SCALE.get("QB", 1.0)
        self.assertAlmostEqual(pts_a, round(12.0 * qb_scale, 2), places=5,
                               msg=f"Expected {round(12.0*qb_scale,2)} under pass_td=6*scale, got {pts_a}")
        self.assertAlmostEqual(pts_b, round(8.0 * qb_scale, 2), places=5,
                               msg=f"Expected {round(8.0*qb_scale,2)} under pass_td=4*scale, got {pts_b}")
        self.assertNotEqual(pts_a, pts_b)

    def test_league_id_and_format_flow_through_to_report_json(self):
        """league_id and league_format kwargs on build_weekly_report_and_pool
        appear as leagueId / leagueFormatAssumption in the report JSON."""
        report, _ = build_weekly_report_and_pool(
            2026, 2, self.config_a, self.pool, self.schedule, self.game_logs, {},
            league_id="league-2", league_format="half_ppr",
        )
        self.assertEqual(report["leagueId"], "league-2")
        self.assertEqual(report["leagueFormatAssumption"], "half_ppr")

    def test_same_player_pool_and_game_logs_used_for_both_leagues(self):
        """Both calls with different configs produce the same player in
        projections -- confirms the shared pool/schedule/game_logs are not
        mutated between calls."""
        report_a, _ = build_weekly_report_and_pool(
            2026, 2, self.config_a, self.pool, self.schedule, self.game_logs, {}
        )
        report_b, _ = build_weekly_report_and_pool(
            2026, 2, self.config_b, self.pool, self.schedule, self.game_logs, {}
        )
        ids_a = {p["playerId"] for p in report_a["projections"]}
        ids_b = {p["playerId"] for p in report_b["projections"]}
        self.assertEqual(ids_a, ids_b)
        self.assertIn("qb-dual-test", ids_a)


if __name__ == "__main__":
    unittest.main()
