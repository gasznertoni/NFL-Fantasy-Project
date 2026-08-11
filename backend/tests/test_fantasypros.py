"""Unit tests for fantasypros.py's pure assembly half (everything except
the `fetch_*`/`load_*` network adapters, per that module's own docstring --
those need network/nflreadpy and are exercised by running
generate_report.py directly, not by this suite). Fixtures below use the
real field names confirmed hands-on 2026-08-11 against the live API for
QB/RB/WR/TE (see fantasypros.py's FANTASYPROS_COLUMN_MAP comment)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasypros import (  # noqa: E402
    build_consensus_tier,
    fantasypros_stats_to_stat_line,
    project_consensus_player,
)

# This league's real config (6-point passing TDs) -- deliberately different
# from FantasyPros' own precomputed `points` field below, so a test that
# only passed by accident (this module silently using their `points`
# instead of recomputing) would fail loudly.
CONFIG = {
    "linear": {
        "pass_yd": 0.04,
        "pass_td": 6,
        "pass_int": -2,
        "rush_yd": 0.1,
        "rush_td": 6,
        "reception": 0.5,
        "fumble_lost": -2,
    }
}

QB_STATS = {
    # Real confirmed field names/shape (Jalen Hurts sample,
    # docs/research/phase1-probe-results.md) -- points/points_ppr/points_half
    # are FantasyPros' own STD scoring (4-pt passing TDs), included here only
    # to prove this module ignores them.
    "points": 23.1,
    "points_ppr": 23.1,
    "points_half": 23.1,
    "pass_att": 27.61,
    "pass_cmp": 18.97,
    "pass_yds": 218.44,
    "pass_tds": 1.57,
    "pass_ints": 0.45,
    "rush_att": 8.87,
    "rush_yds": 40.93,
    "rush_tds": 0.83,
    "fumbles": 0.28,
}


class TestFantasyProsStatsToStatLine(unittest.TestCase):
    def test_maps_confirmed_fields_to_this_projects_category_names(self):
        stat_line = fantasypros_stats_to_stat_line(QB_STATS)
        self.assertEqual(
            stat_line,
            {
                "pass_yd": 218.44,
                "pass_td": 1.57,
                "pass_int": 0.45,
                "rush_yd": 40.93,
                "rush_td": 0.83,
                "fumble_lost": 0.28,
            },
        )

    def test_ignores_unmapped_fields(self):
        stats = {"pass_yds": 200, "pass_att": 30, "pass_cmp": 20, "points": 99, "2pt_tds": 0.1, "ret_tds": 0}
        stat_line = fantasypros_stats_to_stat_line(stats)
        self.assertEqual(stat_line, {"pass_yd": 200})

    def test_receiving_fields_confirmed_for_rb_wr_te(self):
        # Real confirmed field names (Ja'Marr Chase WR sample) -- rec_rec
        # (receptions) maps to this project's "reception" category.
        stats = {"rec_rec": 7.06, "rec_yds": 96.33, "rec_tds": 0.76}
        self.assertEqual(
            fantasypros_stats_to_stat_line(stats),
            {"reception": 7.06, "rec_yd": 96.33, "rec_td": 0.76},
        )

    def test_zero_valued_stats_are_omitted_not_zeroed(self):
        # Matches scoring.nflreadpy_row_to_stat_line's "absent, not zero"
        # contract -- compute_league_points treats a missing category the
        # same as a zero one, so this is a style/consistency choice, not a
        # correctness requirement, but keeps the two adapters symmetric.
        stat_line = fantasypros_stats_to_stat_line({"pass_yds": 0, "pass_tds": 1.5})
        self.assertEqual(stat_line, {"pass_td": 1.5})


class TestProjectConsensusPlayer(unittest.TestCase):
    def test_recomputes_points_from_this_leagues_config_not_fantasypros_points_field(self):
        player = {"fpid": 11594, "name": "Test QB", "position_id": "QB", "team_id": "PHI", "stats": QB_STATS}
        result = project_consensus_player(player, CONFIG)
        # 218.44*0.04 + 1.57*6 + 0.45*-2 + 40.93*0.1 + 0.83*6 + 0.28*-2
        # = 8.7376 + 9.42 - 0.9 + 4.093 + 4.98 - 0.56 = 25.77
        self.assertAlmostEqual(result["projected_points"], 25.77, places=2)
        self.assertNotAlmostEqual(result["projected_points"], QB_STATS["points"], places=1)
        self.assertEqual(result["source"], "consensus")
        self.assertEqual(result["fpid"], 11594)

    def test_missing_stats_object_projects_zero_not_a_crash(self):
        player = {"fpid": 1, "name": "No Stats", "position_id": "QB", "team_id": "FA"}
        result = project_consensus_player(player, CONFIG)
        self.assertEqual(result["projected_points"], 0.0)


class TestBuildConsensusTier(unittest.TestCase):
    def setUp(self):
        self.raw_by_position = {
            "QB": [{"fpid": 11594, "name": "QB One", "position_id": "QB", "team_id": "PHI", "stats": QB_STATS}],
            "WR": [
                {
                    "fpid": 22222,
                    "name": "WR One",
                    "position_id": "WR",
                    "team_id": "CIN",
                    "stats": {"rec_rec": 7, "rec_yds": 90, "rec_tds": 1},
                }
            ],
        }

    def test_resolves_fpid_to_player_id_via_crosswalk(self):
        crosswalk = {11594: "00-1111111", 22222: "00-2222222"}
        tier = build_consensus_tier(self.raw_by_position, CONFIG, crosswalk)
        self.assertEqual(set(tier.keys()), {"00-1111111", "00-2222222"})
        self.assertGreater(tier["00-1111111"]["projected_points"], 0)

    def test_unresolved_fpid_is_dropped_silently(self):
        # Only the WR resolves -- the QB's fpid isn't in this crosswalk,
        # simulating the documented rookie/fantasypros_id-lag gap.
        crosswalk = {22222: "00-2222222"}
        tier = build_consensus_tier(self.raw_by_position, CONFIG, crosswalk)
        self.assertEqual(set(tier.keys()), {"00-2222222"})

    def test_empty_crosswalk_produces_empty_tier(self):
        self.assertEqual(build_consensus_tier(self.raw_by_position, CONFIG, {}), {})


if __name__ == "__main__":
    unittest.main()
