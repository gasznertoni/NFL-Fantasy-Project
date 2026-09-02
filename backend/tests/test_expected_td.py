"""Tests for expected_td.py -- the opportunity-based TD/non-TD split."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from expected_td import (  # noqa: E402
    EXPECTED_TD_POINTS_KEY,
    NON_TD_POINTS_KEY,
    SNAP_SHARE_KEY,
    actual_td_points,
    enrich_game_logs,
    expected_td_points,
    split_game_row,
    td_point_values,
)

# Two leagues that price a passing touchdown differently -- the whole reason
# this module is config-driven rather than using a hardcoded 6.
SIX_POINT_PASS = {"linear": {"pass_yd": 0.04, "pass_td": 6, "rush_td": 6, "rec_td": 6, "reception": 1, "rec_yd": 0.1}}
FOUR_POINT_PASS = {"linear": {"pass_yd": 0.04, "pass_td": 4, "rush_td": 6, "rec_td": 6, "reception": 1, "rec_yd": 0.1}}


class TestTdPointValues(unittest.TestCase):
    def test_reads_each_leagues_own_values(self):
        self.assertEqual(td_point_values(SIX_POINT_PASS)["pass_td"], 6.0)
        self.assertEqual(td_point_values(FOUR_POINT_PASS)["pass_td"], 4.0)

    def test_undefined_category_is_worth_zero(self):
        """An absent rule is worth nothing -- the same reading
        compute_league_points already gives it."""
        values = td_point_values({"linear": {"rush_td": 6}})
        self.assertEqual(values["pass_td"], 0.0)
        self.assertEqual(values["rush_td"], 6.0)


class TestExpectedTdPoints(unittest.TestCase):
    def test_converts_expected_tds_through_league_values(self):
        row = {"pass_touchdown_exp": 2.0, "rush_touchdown_exp": 0.5, "rec_touchdown_exp": 0.0}
        self.assertAlmostEqual(expected_td_points(row, td_point_values(SIX_POINT_PASS)), 15.0)
        self.assertAlmostEqual(expected_td_points(row, td_point_values(FOUR_POINT_PASS)), 11.0)

    def test_missing_and_nan_columns_count_as_zero(self):
        """NaN is truthy and survives a `or 0` guard -- the trap that hid three
        scoring defects in v16. Guard it here too."""
        values = td_point_values(SIX_POINT_PASS)
        self.assertEqual(expected_td_points({}, values), 0.0)
        self.assertEqual(expected_td_points({"pass_touchdown_exp": float("nan")}, values), 0.0)
        self.assertEqual(expected_td_points({"rush_touchdown_exp": "n/a"}, values), 0.0)


class TestSplitGameRow(unittest.TestCase):
    def test_split_is_exact(self):
        """non_td + realised TD points must reconstruct the league total, or the
        decomposition is not a decomposition."""
        from scoring import compute_league_points

        stat_line = {"pass_yd": 300, "pass_td": 3, "rec_yd": 40, "rec_td": 1, "reception": 4,
                     "season": 2025, "week": 3}
        for cfg in (SIX_POINT_PASS, FOUR_POINT_PASS):
            values = td_point_values(cfg)
            row = split_game_row(stat_line, None, values, cfg)
            total = float(compute_league_points(stat_line, cfg))
            self.assertAlmostEqual(
                row[NON_TD_POINTS_KEY] + actual_td_points(stat_line, values), total, places=6
            )

    def test_expected_td_key_absent_without_an_opportunity_row(self):
        """Absent must not be encoded as zero: blend.py flags a missing feature
        separately, and 'no data' is not the same claim as 'no expected
        touchdowns'."""
        row = split_game_row({"rec_yd": 50}, None, td_point_values(SIX_POINT_PASS), SIX_POINT_PASS)
        self.assertIn(NON_TD_POINTS_KEY, row)
        self.assertNotIn(EXPECTED_TD_POINTS_KEY, row)

    def test_does_not_mutate_its_input(self):
        stat_line = {"rec_yd": 50, "rec_td": 1}
        before = dict(stat_line)
        split_game_row(stat_line, None, td_point_values(SIX_POINT_PASS), SIX_POINT_PASS)
        self.assertEqual(stat_line, before)

    def test_split_keys_are_not_scored(self):
        """The added keys ride along on the stat line and must stay invisible to
        compute_league_points, which reads only categories in the config."""
        from scoring import compute_league_points

        stat_line = {"rec_yd": 50, "rec_td": 1}
        plain = float(compute_league_points(stat_line, SIX_POINT_PASS))
        enriched = split_game_row(
            stat_line, {"rec_touchdown_exp": 0.7}, td_point_values(SIX_POINT_PASS), SIX_POINT_PASS
        )
        self.assertAlmostEqual(float(compute_league_points(enriched, SIX_POINT_PASS)), plain)


class TestEnrichGameLogs(unittest.TestCase):
    def _logs(self):
        return {"p1": [{"season": 2025, "week": 1, "rec_yd": 80, "rec_td": 1, "reception": 6}]}

    def test_adds_all_three_features_when_data_is_present(self):
        row = enrich_game_logs(
            self._logs(),
            {(2025, 1, "p1"): {"rec_touchdown_exp": 0.8}},
            {(2025, 1, "p1"): 0.91},
            SIX_POINT_PASS,
        )["p1"][0]
        self.assertAlmostEqual(row[EXPECTED_TD_POINTS_KEY], 4.8)
        self.assertAlmostEqual(row[NON_TD_POINTS_KEY], 14.0)  # 8 + 6 receptions
        self.assertAlmostEqual(row[SNAP_SHARE_KEY], 0.91)

    def test_missing_lookups_leave_features_absent_not_zero(self):
        row = enrich_game_logs(self._logs(), {}, {}, SIX_POINT_PASS)["p1"][0]
        self.assertIn(NON_TD_POINTS_KEY, row)
        self.assertNotIn(EXPECTED_TD_POINTS_KEY, row)
        self.assertNotIn(SNAP_SHARE_KEY, row)

    def test_every_player_and_game_survives(self):
        logs = {"a": [{"season": 2025, "week": w} for w in (1, 2, 3)], "b": [{"season": 2025, "week": 1}]}
        out = enrich_game_logs(logs, {}, {}, SIX_POINT_PASS)
        self.assertEqual(sorted(out), ["a", "b"])
        self.assertEqual(len(out["a"]), 3)


class TestRealLeagueConfigs(unittest.TestCase):
    """The two shipped configs really do price touchdowns differently, which is
    what makes the per-league split necessary rather than decorative."""

    def _cfg(self, name):
        path = Path(__file__).resolve().parents[1] / "leagues" / name / "scoring-config.json"
        return json.loads(path.read_text())

    def test_league_configs_disagree_on_passing_tds(self):
        self.assertEqual(td_point_values(self._cfg("league-1"))["pass_td"], 6.0)
        self.assertEqual(td_point_values(self._cfg("league-2"))["pass_td"], 4.0)

    def test_same_expected_row_scores_differently_per_league(self):
        row = {"pass_touchdown_exp": 1.5, "rush_touchdown_exp": 0.2, "rec_touchdown_exp": 0.0}
        one = expected_td_points(row, td_point_values(self._cfg("league-1")))
        two = expected_td_points(row, td_point_values(self._cfg("league-2")))
        self.assertAlmostEqual(one, 10.2)
        self.assertAlmostEqual(two, 7.2)


if __name__ == "__main__":
    unittest.main()


class TestEstimatorSubstitution(unittest.TestCase):
    """projections._estimator_series -- built, validated, and default OFF.

    It beat a bare rolling average standalone but does not survive integration
    against the full stack (sign flips between held-out seasons, p>0.13
    throughout). These tests keep the code honest so the flag can be flipped
    without re-deriving it. See projections.USE_OPPORTUNITY_TD_ESTIMATOR.
    """

    def test_default_is_off(self):
        import projections

        self.assertFalse(
            projections.USE_OPPORTUNITY_TD_ESTIMATOR,
            "enabling this needs a fresh held-out measurement -- it did not "
            "replicate through the real pipeline in 2026-09",
        )

    def test_substitutes_only_where_both_halves_exist(self):
        from projections import _estimator_series

        recent = [
            {NON_TD_POINTS_KEY: 8.0, EXPECTED_TD_POINTS_KEY: 3.0},   # substituted
            {NON_TD_POINTS_KEY: 8.0},                                 # no expected TD
            {},                                                       # neither
        ]
        self.assertEqual(_estimator_series(recent, [20.0, 21.0, 22.0]), [11.0, 21.0, 22.0])

    def test_nan_falls_back_to_the_real_total(self):
        from projections import _estimator_series

        recent = [{NON_TD_POINTS_KEY: float("nan"), EXPECTED_TD_POINTS_KEY: 3.0}]
        self.assertEqual(_estimator_series(recent, [17.0]), [17.0])

    def test_display_series_is_untouched(self):
        """per_game_points is what the report shows behind a projection, so it
        must stay the player's real weekly scores whatever the estimator does."""
        from projections import _estimator_series

        points = [12.0, 5.0]
        recent = [{NON_TD_POINTS_KEY: 1.0, EXPECTED_TD_POINTS_KEY: 1.0}] * 2
        _estimator_series(recent, points)
        self.assertEqual(points, [12.0, 5.0])
