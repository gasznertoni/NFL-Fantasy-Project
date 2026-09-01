"""Unit tests for backend/context.py -- the Vegas-derived game context.

The sign convention is the whole risk here: nflverse quotes spread_line from
the home team's perspective, and getting it backwards produces perfectly
plausible numbers with the favourite and the underdog swapped. These tests pin
it in both directions.
"""

import unittest

from context import (
    DEFAULT_IMPLIED_TOTAL,
    DEFAULT_TOTAL_LINE,
    context_features,
    game_context_by_team,
    implied_totals,
)

GAME = {"home_team": "KC", "away_team": "BUF", "total_line": 48.0, "spread_line": 3.0}


class TestImpliedTotals(unittest.TestCase):
    def test_home_favourite_gets_the_larger_share(self):
        home, away = implied_totals(48.0, 3.0)
        self.assertAlmostEqual(home, 25.5)
        self.assertAlmostEqual(away, 22.5)
        self.assertGreater(home, away)

    def test_home_underdog_gets_the_smaller_share(self):
        home, away = implied_totals(48.0, -3.0)
        self.assertAlmostEqual(home, 22.5)
        self.assertAlmostEqual(away, 25.5)

    def test_the_two_halves_sum_to_the_total(self):
        home, away = implied_totals(44.5, 7.5)
        self.assertAlmostEqual(home + away, 44.5)

    def test_a_pick_em_splits_evenly(self):
        self.assertEqual(implied_totals(50.0, 0.0), (25.0, 25.0))

    def test_unpriced_game_returns_none(self):
        self.assertIsNone(implied_totals(None, 3.0))
        self.assertIsNone(implied_totals(48.0, None))
        self.assertIsNone(implied_totals(float("nan"), 3.0))


class TestGameContextByTeam(unittest.TestCase):
    def setUp(self):
        self.context = game_context_by_team([GAME])

    def test_both_teams_are_present_with_each_other_as_opponent(self):
        self.assertEqual(self.context["KC"]["opponent"], "BUF")
        self.assertEqual(self.context["BUF"]["opponent"], "KC")

    def test_home_flag(self):
        self.assertEqual(self.context["KC"]["is_home"], 1.0)
        self.assertEqual(self.context["BUF"]["is_home"], 0.0)

    def test_implied_totals_are_attached_per_team(self):
        self.assertAlmostEqual(self.context["KC"]["implied_team_total"], 25.5)
        self.assertAlmostEqual(self.context["BUF"]["implied_team_total"], 22.5)

    def test_opponent_implied_total_is_the_mirror(self):
        self.assertAlmostEqual(self.context["KC"]["opponent_implied_total"], 22.5)
        self.assertAlmostEqual(self.context["BUF"]["opponent_implied_total"], 25.5)

    def test_spread_is_flipped_so_positive_always_means_favoured(self):
        # Without the flip the same number means opposite things depending on
        # which row a player happened to be in -- a silent, plausible-looking
        # sign error inside a fitted model.
        self.assertEqual(self.context["KC"]["spread_line"], 3.0)
        self.assertEqual(self.context["BUF"]["spread_line"], -3.0)

    def test_unpriced_game_still_yields_teams_with_none_totals(self):
        context = game_context_by_team([{"home_team": "A", "away_team": "B"}])
        self.assertIsNone(context["A"]["implied_team_total"])
        self.assertEqual(context["A"]["opponent"], "B")

    def test_rows_without_teams_are_skipped(self):
        self.assertEqual(game_context_by_team([{"total_line": 40.0}]), {})


class TestContextFeatures(unittest.TestCase):
    def test_extracts_the_numeric_features(self):
        features = context_features(game_context_by_team([GAME])["KC"])
        self.assertAlmostEqual(features["implied_team_total"], 25.5)
        self.assertAlmostEqual(features["total_line"], 48.0)
        self.assertAlmostEqual(features["spread_line"], 3.0)
        self.assertEqual(features["is_home"], 1.0)

    def test_missing_context_falls_back_to_league_average(self):
        features = context_features(None)
        self.assertAlmostEqual(features["implied_team_total"], DEFAULT_IMPLIED_TOTAL)
        self.assertAlmostEqual(features["total_line"], DEFAULT_TOTAL_LINE)
        self.assertEqual(features["spread_line"], 0.0)

    def test_partially_priced_context_fills_only_what_is_missing(self):
        features = context_features({"total_line": 52.0, "implied_team_total": None})
        self.assertAlmostEqual(features["total_line"], 52.0)
        self.assertAlmostEqual(features["implied_team_total"], DEFAULT_IMPLIED_TOTAL)

    def test_every_feature_is_a_finite_number(self):
        for value in (None, {}, {"implied_team_total": float("nan")}):
            for feature in context_features(value).values():
                self.assertIsInstance(feature, float)
                self.assertEqual(feature, feature)  # not NaN


if __name__ == "__main__":
    unittest.main()
