"""Unit tests for scoring.py, per design spec section 6: hand-built stat
lines with known expected output, run before this logic feeds any
projection so a scoring-formula bug is caught here, not downstream."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import compute_league_points, nflreadpy_row_to_stat_line  # noqa: E402

CONFIG = {
    "linear": {"pass_yd": 0.04, "pass_td": 6, "pass_int": -2, "reception": 0.5, "rec_yd": 0.1},
    "milestones": {
        "pass_yd": {
            "mode": "highest",
            "tiers": [
                {"threshold": 300, "points": 3},
                {"threshold": 400, "points": 5},
            ],
        }
    },
    "tiers": {
        "def_points_allowed": [
            {"max": 0, "points": 10},
            {"max": 6, "points": 7},
            {"max": 999, "points": -4},
        ]
    },
}


class TestLinearScoring(unittest.TestCase):
    def test_basic_qb_line(self):
        stat_line = {"pass_yd": 275, "pass_td": 2, "pass_int": 1}
        result = compute_league_points(stat_line, CONFIG)
        # 275*0.04 = 11.0, 2*6 = 12, 1*-2 = -2 -> 21.0, no milestone (under 300)
        self.assertAlmostEqual(result.total, 21.0)
        self.assertNotIn("pass_yd_milestone", result.breakdown)

    def test_six_point_passing_td_is_used_not_four(self):
        """The one confirmed-real value per CLAUDE.md -- guards against
        accidentally reverting to a generic 4-point default."""
        result = compute_league_points({"pass_td": 1}, CONFIG)
        self.assertEqual(result.breakdown["pass_td"], 6)

    def test_missing_category_contributes_zero_not_error(self):
        stat_line = {"reception": 5, "rec_yd": 60}
        result = compute_league_points(stat_line, CONFIG)  # no pass_yd/pass_td/pass_int at all
        self.assertAlmostEqual(result.total, 5 * 0.5 + 60 * 0.1)

    def test_zero_stat_contributes_nothing_to_breakdown(self):
        result = compute_league_points({"pass_td": 0, "reception": 3}, CONFIG)
        self.assertNotIn("pass_td", result.breakdown)


class TestMilestoneScoring(unittest.TestCase):
    def test_under_threshold_no_bonus(self):
        result = compute_league_points({"pass_yd": 299}, CONFIG)
        self.assertNotIn("pass_yd_milestone", result.breakdown)

    def test_exactly_at_threshold_gets_bonus(self):
        result = compute_league_points({"pass_yd": 300}, CONFIG)
        self.assertEqual(result.breakdown["pass_yd_milestone"], 3)

    def test_highest_mode_uses_only_top_tier_not_both(self):
        result = compute_league_points({"pass_yd": 450}, CONFIG)
        self.assertEqual(result.breakdown["pass_yd_milestone"], 5)  # not 3+5=8

    def test_cumulative_mode_sums_all_met_tiers(self):
        cumulative_config = {
            "milestones": {
                "pass_yd": {
                    "mode": "cumulative",
                    "tiers": [
                        {"threshold": 300, "points": 3},
                        {"threshold": 400, "points": 5},
                    ],
                }
            }
        }
        result = compute_league_points({"pass_yd": 450}, cumulative_config)
        self.assertEqual(result.breakdown["pass_yd_milestone"], 8)

    def test_mode_lives_once_per_category_not_per_tier(self):
        """Regression: mode used to be read off tiers[0] and silently
        ignored on every other tier entry -- a config that only sets mode
        in one place now can't disagree with itself."""
        config = {
            "milestones": {
                "pass_yd": {
                    "mode": "cumulative",
                    "tiers": [{"threshold": 300, "points": 3}, {"threshold": 400, "points": 5}],
                }
            }
        }
        result = compute_league_points({"pass_yd": 450}, config)
        self.assertEqual(result.breakdown["pass_yd_milestone"], 8)


class TestTierScoring(unittest.TestCase):
    def test_shutout_gets_top_tier(self):
        result = compute_league_points({"def_points_allowed": 0}, CONFIG)
        self.assertEqual(result.breakdown["def_points_allowed"], 10)

    def test_blowout_loss_gets_worst_tier(self):
        result = compute_league_points({"def_points_allowed": 45}, CONFIG)
        self.assertEqual(result.breakdown["def_points_allowed"], -4)

    def test_middle_band_boundary(self):
        result = compute_league_points({"def_points_allowed": 6}, CONFIG)
        self.assertEqual(result.breakdown["def_points_allowed"], 7)


class TestKickerScoring(unittest.TestCase):
    """Kicker categories are plain linear config, same mechanism as offense
    -- these just confirm the category names round-trip correctly, not any
    new scoring logic."""

    def test_field_goals_and_pat_score_via_linear_config(self):
        config = {
            "linear": {
                "fg_made_0_39": 3,
                "fg_made_40_49": 4,
                "fg_made_50_plus": 5,
                "fg_missed": -1,
                "pat_made": 1,
                "pat_missed": -1,
            }
        }
        stat_line = {"fg_made_0_39": 1, "fg_made_50_plus": 1, "fg_missed": 1, "pat_made": 3}
        result = compute_league_points(stat_line, config)
        self.assertAlmostEqual(result.total, 3 + 5 - 1 + 3)


class TestNflreadpyColumnMap(unittest.TestCase):
    def test_maps_known_columns(self):
        row = {"passing_yards": 300, "passing_tds": 2, "receptions": 5, "receiving_yards": 60}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped, {"pass_yd": 300, "pass_td": 2, "reception": 5, "rec_yd": 60})

    def test_sums_fumble_lost_across_three_source_columns(self):
        row = {"sack_fumbles_lost": 1, "rushing_fumbles_lost": 1, "receiving_fumbles_lost": 0}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped["fumble_lost"], 2)

    def test_sums_fumble_across_three_source_columns_independent_of_lost(self):
        # Added 2026-08-16 alongside the "fumble" (total, not just lost)
        # category -- the real league scores a plain fumble (-1) AND a
        # fumble lost (-2) as separate, stacking categories, so a fumble
        # that WAS lost must populate both "fumble" and "fumble_lost"
        # simultaneously from the same row, not just whichever happened.
        row = {
            "sack_fumbles": 1,
            "rushing_fumbles": 1,
            "receiving_fumbles": 0,
            "rushing_fumbles_lost": 1,
        }
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped["fumble"], 2)
        self.assertEqual(mapped["fumble_lost"], 1)

    def test_zero_and_missing_values_are_skipped(self):
        row = {"passing_yards": 0, "passing_tds": None}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped, {})


if __name__ == "__main__":
    unittest.main()
