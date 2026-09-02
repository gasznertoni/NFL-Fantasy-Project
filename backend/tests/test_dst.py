"""Unit tests for dst.py's pure assembly half -- hand-built team_stats/
schedule rows with known expected output, same pattern as test_scoring.py.
No network (the load_* adapters at the bottom of dst.py are NOT exercised
here -- see that module's own docstring)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dst import (  # noqa: E402
    assemble_dst_stat_line,
    build_dst_game_logs,
    normalize_team,
)


class TestNormalizeTeam(unittest.TestCase):
    def test_maps_la_to_lar(self):
        self.assertEqual(normalize_team("LA"), "LAR")

    def test_passes_through_unmapped_teams(self):
        self.assertEqual(normalize_team("BUF"), "BUF")


class TestAssembleDstStatLine(unittest.TestCase):
    def test_direct_fields_read_from_own_row(self):
        own_row = {
            "def_sacks": 3.0,
            "def_interceptions": 2,
            "fumble_recovery_opp": 1,
            "def_safeties": 1,
            "def_fumbles_forced": 2,
        }
        opponent_row = {}
        stat_line = assemble_dst_stat_line(own_row, opponent_row, points_allowed=17)
        self.assertEqual(stat_line["def_sack"], 3.0)
        self.assertEqual(stat_line["def_int"], 2)
        self.assertEqual(stat_line["def_fumble_rec"], 1)
        self.assertEqual(stat_line["def_safety"], 1)
        self.assertEqual(stat_line["fumble_forced"], 2)

    def test_three_td_concepts_map_to_three_distinct_categories(self):
        """The three touchdown columns stay separate, so each league's config
        can score exactly the rule it has.

        Until 2026-09-02 fumble_recovery_tds was emitted as "def_st_td", which
        was only correct because league-1 was the sole league and its one
        TD-credit line is ESPN's 'Fumble Recovered for TD (FTD) = 6'. league-2's
        real settings score a general 'Defense TD' AND a separate 'Special
        teams td', so the names now mean what they say. See
        docs/research/dst-td-decomposition.md and the DST_DIRECT_COLUMN_MAP
        comment."""
        own_row = {"fumble_recovery_tds": 1, "def_tds": 2, "special_teams_tds": 3}
        stat_line = assemble_dst_stat_line(own_row, {}, points_allowed=0)
        self.assertEqual(stat_line["def_fumble_rec_td"], 1)
        self.assertEqual(stat_line["def_td"], 2)
        self.assertEqual(stat_line["def_st_td"], 3)

    def test_absent_td_columns_produce_no_category(self):
        """A league scores only the keys its config defines, so an absent
        source column must leave the category out entirely rather than
        emitting a zero that a config could mistake for a real result."""
        stat_line = assemble_dst_stat_line({"def_tds": 1}, {}, points_allowed=0)
        self.assertEqual(stat_line["def_td"], 1)
        self.assertNotIn("def_fumble_rec_td", stat_line)
        self.assertNotIn("def_st_td", stat_line)

    def test_overlapping_td_categories_rejected(self):
        """def_td already includes fumble-return scores, so a config defining
        both would double-count them. Same failure class as the v16 column-map
        defects: silent, plausible, and wrong -- so it raises."""
        from dst import validate_dst_td_categories
        validate_dst_td_categories({"def_td": 6})
        validate_dst_td_categories({"def_fumble_rec_td": 6})
        with self.assertRaises(ValueError):
            validate_dst_td_categories({"def_td": 6, "def_fumble_rec_td": 6})

    def test_return_yards_sums_punt_and_kickoff(self):
        own_row = {"punt_return_yards": 15, "kickoff_return_yards": 22}
        stat_line = assemble_dst_stat_line(own_row, {}, points_allowed=0)
        self.assertEqual(stat_line["def_return_yd"], 37)

    def test_blocked_kick_credit_reads_opponent_row_not_own_row(self):
        """Regression guard for the doc's key finding: blocks are recorded
        on the row of the team whose kick got blocked, not the blocker --
        crediting the defense means reading the OPPONENT's fg_blocked/
        pt_blocked, not its own (which would silently always be zero for a
        real defense, since a team doesn't block its own kicks)."""
        own_row = {"fg_blocked": 0, "pt_blocked": 0}
        opponent_row = {"fg_blocked": 1, "pt_blocked": 0}
        stat_line = assemble_dst_stat_line(own_row, opponent_row, points_allowed=0)
        self.assertEqual(stat_line["def_blocked_kick"], 1)

    def test_yards_allowed_sums_opponents_passing_and_rushing(self):
        own_row = {"passing_yards": 999, "rushing_yards": 999}  # own offense, must be ignored
        opponent_row = {"passing_yards": 214, "rushing_yards": 107}
        stat_line = assemble_dst_stat_line(own_row, opponent_row, points_allowed=13)
        self.assertEqual(stat_line["def_yards_allowed"], 321)

    def test_points_allowed_passed_through_even_when_zero(self):
        """A shutout (0 points allowed) is real, valuable information for
        the banded tier scoring -- must not be treated as 'no data' and
        skipped the way a falsy linear-category value is."""
        stat_line = assemble_dst_stat_line({}, {}, points_allowed=0)
        self.assertEqual(stat_line["def_points_allowed"], 0)
        self.assertIn("def_yards_allowed", stat_line)

    def test_zero_direct_stats_are_omitted_not_zero_filled(self):
        """Matches scoring.py's _linear_points contract: a category absent
        from the stat line contributes 0 via compute_league_points, so
        there's no need to materialize explicit zeros here."""
        stat_line = assemble_dst_stat_line({"def_sacks": 0}, {}, points_allowed=10)
        self.assertNotIn("def_sack", stat_line)


class TestBuildDstGameLogs(unittest.TestCase):
    """Synthetic two-team, one-game fixture mirroring the real ARI@NO 2025
    week-1 shape confirmed by hand against live nflreadpy data."""

    TEAM_STATS = [
        {
            "season": 2025, "week": 1, "team": "ARI", "opponent_team": "NO", "game_id": "2025_01_ARI_NO",
            "def_sacks": 1.0, "def_interceptions": 0, "fumble_recovery_opp": 0, "def_safeties": 0,
            "def_fumbles_forced": 0, "passing_yards": 163, "rushing_yards": 146, "fg_blocked": 0, "pt_blocked": 0,
        },
        {
            "season": 2025, "week": 1, "team": "NO", "opponent_team": "ARI", "game_id": "2025_01_ARI_NO",
            "def_sacks": 2.0, "def_interceptions": 1, "fumble_recovery_opp": 0, "def_safeties": 0,
            "def_fumbles_forced": 1, "passing_yards": 214, "rushing_yards": 107, "fg_blocked": 0, "pt_blocked": 0,
        },
    ]
    SCHEDULE = [
        {"game_id": "2025_01_ARI_NO", "season": 2025, "week": 1, "home_team": "NO", "away_team": "ARI",
         "home_score": 13, "away_score": 20},
    ]

    def test_points_allowed_direction_matches_real_confirmed_game(self):
        """ARI (away) scored 20, NO (home) scored 13 -- confirmed against
        the real 2025 week-1 ARI@NO game. ARI's defense allowed NO's 13
        points, not ARI's own 20."""
        game_logs = build_dst_game_logs(self.TEAM_STATS, self.SCHEDULE)
        ari_game = game_logs["ARI"][0]
        no_game = game_logs["NO"][0]
        self.assertEqual(ari_game["def_points_allowed"], 13)
        self.assertEqual(no_game["def_points_allowed"], 20)

    def test_yards_allowed_uses_opponent_offense(self):
        game_logs = build_dst_game_logs(self.TEAM_STATS, self.SCHEDULE)
        ari_game = game_logs["ARI"][0]
        self.assertEqual(ari_game["def_yards_allowed"], 214 + 107)

    def test_output_shape_matches_project_player_game_log_contract(self):
        game_logs = build_dst_game_logs(self.TEAM_STATS, self.SCHEDULE)
        ari_game = game_logs["ARI"][0]
        self.assertEqual(ari_game["season"], 2025)
        self.assertEqual(ari_game["week"], 1)
        self.assertEqual(ari_game["opponent_team"], "NO")

    def test_missing_opponent_row_is_skipped_not_guessed(self):
        one_sided = [self.TEAM_STATS[0]]  # NO's row missing entirely
        game_logs = build_dst_game_logs(one_sided, self.SCHEDULE)
        self.assertEqual(game_logs, {})

    def test_missing_schedule_row_is_skipped_not_guessed(self):
        game_logs = build_dst_game_logs(self.TEAM_STATS, [])
        self.assertEqual(game_logs, {})

    def test_keyed_by_team_abbreviation_not_gsis_id(self):
        game_logs = build_dst_game_logs(self.TEAM_STATS, self.SCHEDULE)
        self.assertEqual(set(game_logs.keys()), {"ARI", "NO"})


if __name__ == "__main__":
    unittest.main()
