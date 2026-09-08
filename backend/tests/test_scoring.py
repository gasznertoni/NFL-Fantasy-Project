"""Unit tests for scoring.py, per design spec section 6: hand-built stat
lines with known expected output, run before this logic feeds any
projection so a scoring-formula bug is caught here, not downstream."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import (  # noqa: E402
    compute_league_points,
    nflreadpy_row_to_stat_line,
    resolve_value,
)

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

    def test_fumbles_come_from_nflreadpy_whole_player_totals(self):
        # Corrected 2026-09-01. These used to be summed from
        # sack_/rushing_/receiving_fumbles(_lost), which are a proper SUBSET
        # of the player's fumbles: over 2023-25 they miss 15.6% of
        # fumbles_total and 10.6% of fumbles_lost_total (a fumble on a
        # return, a lateral, an aborted snap). nflreadpy carries the exact
        # whole-player totals, so we read those instead of rebuilding them.
        # A lost fumble still populates BOTH categories -- the real league
        # scores a plain fumble (-1) and a fumble lost (-2) as separate,
        # stacking lines -- which the totals preserve.
        row = {"fumbles_total": 2, "fumbles_lost_total": 1}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped["fumble"], 2)
        self.assertEqual(mapped["fumble_lost"], 1)

    def test_partial_fumble_columns_are_no_longer_read(self):
        # Guards the correction above: the old subset columns must not
        # silently contribute, or a fumble would be counted twice.
        row = {"sack_fumbles": 1, "rushing_fumbles": 1, "rushing_fumbles_lost": 1}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertNotIn("fumble", mapped)
        self.assertNotIn("fumble_lost", mapped)

    def test_interceptions_map_from_passing_interceptions(self):
        # Regression guard for the 2026-09-01 audit: the map keyed on
        # "interceptions", which nflreadpy does not emit, so pass_int's -2
        # never applied and every QB scored ~1.4 pts/game too high.
        row = {"passing_interceptions": 2}
        self.assertEqual(nflreadpy_row_to_stat_line(row)["pass_int"], 2)
        self.assertNotIn("pass_int", nflreadpy_row_to_stat_line({"interceptions": 2}))

    def test_return_touchdowns_are_credited_to_the_player(self):
        row = {"special_teams_tds": 1}
        self.assertEqual(nflreadpy_row_to_stat_line(row)["return_td"], 1)

    def test_zero_and_missing_values_are_skipped(self):
        row = {"passing_yards": 0, "passing_tds": None}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped, {})


class TestPositionScopedValues(unittest.TestCase):
    """league-1's final ESPN settings (2026-09-06) price several categories by
    position -- a TE reception is 1.0 where an RB/WR reception is 0.5, and the
    receiving-yardage milestones pay TE > RB/WR > QB. Before that every value
    in a config was a plain number."""

    CFG = {
        "linear": {"reception": {"TE": 1, "default": 0.5}, "rec_yd": 0.1},
        "milestones": {"rec_yd": {"mode": "highest", "tiers": [
            {"threshold": 100, "points": {"TE": 1, "QB": 0, "default": 0.75}}]}},
    }

    def test_resolve_value_passes_plain_numbers_through(self):
        self.assertEqual(resolve_value(0.5, "TE"), 0.5)
        self.assertEqual(resolve_value(-2, None), -2)

    def test_resolve_value_prefers_an_exact_position_then_default(self):
        value = {"TE": 1, "default": 0.5}
        self.assertEqual(resolve_value(value, "TE"), 1)
        self.assertEqual(resolve_value(value, "WR"), 0.5)
        self.assertEqual(resolve_value(value, None), 0.5)

    def test_resolve_value_scores_nothing_when_nothing_matches(self):
        # Matches the module's standing "an absent rule contributes 0" contract
        # rather than raising -- e.g. rec_td_40 is {"TE": 1} with no default.
        self.assertEqual(resolve_value({"TE": 1}, "WR"), 0)

    def test_tight_end_reception_premium_changes_the_total(self):
        line = {"reception": 6, "rec_yd": 80}
        self.assertEqual(compute_league_points(line, self.CFG, position="TE").total, 14.0)
        self.assertEqual(compute_league_points(line, self.CFG, position="WR").total, 11.0)

    def test_position_is_read_off_the_stat_line_by_default(self):
        # projections.load_full_pool_game_logs, dst.assemble_dst_stat_line and
        # kicker.nflreadpy_kicker_row_to_stat_line all set stat_line["position"],
        # so two-argument callers stay correct without being touched.
        line = {"reception": 6, "rec_yd": 80, "position": "TE"}
        self.assertEqual(compute_league_points(line, self.CFG).total, 14.0)

    def test_explicit_position_argument_overrides_the_stat_line(self):
        line = {"reception": 6, "rec_yd": 80, "position": "TE"}
        self.assertEqual(compute_league_points(line, self.CFG, position="WR").total, 11.0)

    def test_milestone_points_are_position_scoped_too(self):
        line = {"rec_yd": 120, "reception": 0}
        for position, expected in (("TE", 13.0), ("WR", 12.75), ("QB", 12.0)):
            self.assertAlmostEqual(
                compute_league_points(line, self.CFG, position=position).total, expected)


class TestFinalLeagueOneColumns(unittest.TestCase):
    """Columns added 2026-09-06 for league-1's final settings. Each was checked
    against a live load_player_stats() response first -- a key matching nothing
    is silent, which is how three defects survived the v16 audit."""

    def test_completions_sacks_and_first_downs_map(self):
        row = {"completions": 22, "sacks_suffered": 3,
               "rushing_first_downs": 4, "receiving_first_downs": 5}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped["pass_completion"], 22)
        self.assertEqual(mapped["pass_sacked"], 3)
        self.assertEqual(mapped["rush_first_down"], 4)
        self.assertEqual(mapped["rec_first_down"], 5)

    def test_return_yardage_maps_for_the_player_side(self):
        row = {"kickoff_return_yards": 61, "punt_return_yards": 18}
        mapped = nflreadpy_row_to_stat_line(row)
        self.assertEqual(mapped["kick_return_yd"], 61)
        self.assertEqual(mapped["punt_return_yd"], 18)

    def test_incompletions_are_derived_from_attempts_minus_completions(self):
        mapped = nflreadpy_row_to_stat_line({"attempts": 35, "completions": 22})
        self.assertEqual(mapped["pass_incompletion"], 13)

    def test_a_perfect_game_records_no_incompletions(self):
        mapped = nflreadpy_row_to_stat_line({"attempts": 4, "completions": 4})
        self.assertNotIn("pass_incompletion", mapped)

    def test_incompletions_never_go_negative(self):
        # Defensive: a completions > attempts row would otherwise turn into a
        # positive credit at -0.1 a unit.
        mapped = nflreadpy_row_to_stat_line({"attempts": 3, "completions": 5})
        self.assertNotIn("pass_incompletion", mapped)

    def test_nan_attempts_do_not_produce_a_nan_incompletion(self):
        mapped = nflreadpy_row_to_stat_line({"attempts": float("nan"), "completions": 4})
        self.assertNotIn("pass_incompletion", mapped)

    def test_position_rides_along_on_the_stat_line(self):
        self.assertEqual(nflreadpy_row_to_stat_line({"position": "TE"})["position"], "TE")
        self.assertNotIn("position", nflreadpy_row_to_stat_line({"position": float("nan")}))

    def test_a_nan_stat_cell_is_skipped_rather_than_poisoning_the_total(self):
        result = compute_league_points({"pass_yd": float("nan"), "pass_td": 2},
                                       {"linear": {"pass_yd": 0.05, "pass_td": 4}})
        self.assertEqual(result.total, 8.0)


class TestRealLeagueOneConfigEndToEnd(unittest.TestCase):
    """One realistic game per position through the actual shipped config, so a
    hand-edit to the JSON that changes a total shows up as a failing number."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "leagues" / "league-1" / "scoring-config.json"
        cls.cfg = json.loads(path.read_text())

    def test_quarterback_line(self):
        # 300 yds, 22/35, 2 TD, 1 INT, 2 sacks: 15 + 2.2 - 1.3 + 8 - 2 - 1 = 20.9
        line = {"position": "QB", "pass_yd": 300, "pass_completion": 22,
                "pass_incompletion": 13, "pass_td": 2, "pass_int": 1, "pass_sacked": 2}
        self.assertAlmostEqual(compute_league_points(line, self.cfg).total, 20.9)

    def test_the_same_receiving_line_pays_a_tight_end_more(self):
        line = {"rec_yd": 80, "reception": 6, "rec_first_down": 4, "rec_td": 1}
        te = compute_league_points(line, self.cfg, position="TE").total
        wr = compute_league_points(line, self.cfg, position="WR").total
        # 8 yds + 3 first downs + 6 TD = 17 shared; receptions add 6 or 3.
        self.assertAlmostEqual(te, 23.0)
        self.assertAlmostEqual(wr, 20.0)
        self.assertAlmostEqual(te - wr, 3.0)   # 6 receptions x the 0.5 premium

    def test_rushing_milestone_takes_the_highest_band_not_the_sum(self):
        # 210 yards clears both the 100 and 200 bands; ESPN's bands read
        # "100-199" and "200+", so this pays 2, not 2.75.
        line = {"position": "RB", "rush_yd": 210}
        self.assertAlmostEqual(compute_league_points(line, self.cfg).total, 23.0)

    def test_a_long_missed_field_goal_is_free(self):
        # league-1 defines no fg_missed_50_59 line at all, so it scores nothing.
        line = {"position": "K", "fg_made_50_59": 1, "fg_missed_50_59": 1}
        self.assertAlmostEqual(compute_league_points(line, self.cfg).total, 5.0)

    def test_passing_touchdowns_are_worth_four(self):
        # Regression guard: this league paid 6 until 2026-09-06 and CLAUDE.md
        # described the 6 as one of its defining features.
        self.assertEqual(self.cfg["linear"]["pass_td"], 4)


if __name__ == "__main__":
    unittest.main()
