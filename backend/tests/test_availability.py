"""Unit tests for backend/availability.py.

The nflreadpy adapters (load_injury_report_nflreadpy,
build_training_rows_nflreadpy) are NOT covered -- they need the network. Their
real-data validation is in docs/research/scoring-engine-and-model-audit.md.
"""

import unittest

from availability import (
    ALWAYS_AVAILABLE_POSITIONS,
    LEAGUE_PLAY_RATE,
    AvailabilityModel,
    expected_points,
    play_rate_history,
)


def rows(n_out=60, n_healthy=240):
    """Synthetic training rows with the real signal's shape: an Out
    designation is near-certain, everyone else mostly plays."""
    out = []
    for i in range(n_out):
        out.append({
            "player_id": f"out{i}", "position": "WR", "played": 0,
            "report_status": "Out", "practice_status": "Did Not Participate In Practice",
            "prior_play_rate": 0.8, "prior_games_observed": 8.0,
        })
    for i in range(n_healthy):
        out.append({
            "player_id": f"ok{i}", "position": "WR", "played": 1,
            "report_status": None, "practice_status": "Full Participation in Practice",
            "prior_play_rate": 0.9, "prior_games_observed": 8.0,
        })
    return out


class TestExpectedPoints(unittest.TestCase):
    def test_multiplies_by_the_probability(self):
        self.assertAlmostEqual(expected_points(12.0, 0.5), 6.0)

    def test_certainty_is_the_identity(self):
        self.assertAlmostEqual(expected_points(12.0, 1.0), 12.0)

    def test_a_player_who_cannot_play_is_worth_exactly_zero(self):
        # Not an approximation: a player who does not take the field scores 0.
        self.assertEqual(expected_points(25.0, 0.0), 0.0)

    def test_probability_is_clamped_to_the_unit_interval(self):
        self.assertAlmostEqual(expected_points(10.0, 1.4), 10.0)
        self.assertAlmostEqual(expected_points(10.0, -0.3), 0.0)


class TestPlayRateHistory(unittest.TestCase):
    def test_rate_is_over_the_teams_weeks_not_a_week_range(self):
        # The team's bye is week 3, so weeks 1,2,4 are the denominator. Using
        # range(1, 5) instead would count the bye as a missed game and drag
        # every player on that team down by a week.
        history = play_rate_history({"p": {1, 2}}, team_weeks={1, 2, 4}, as_of_week=5)
        self.assertAlmostEqual(history["p"]["prior_play_rate"], 2 / 3)
        self.assertEqual(history["p"]["prior_games_observed"], 3.0)

    def test_only_weeks_before_as_of_count(self):
        history = play_rate_history({"p": {1, 2, 3}}, team_weeks={1, 2, 3}, as_of_week=3)
        self.assertAlmostEqual(history["p"]["prior_play_rate"], 1.0)
        self.assertEqual(history["p"]["prior_games_observed"], 2.0)

    def test_week_one_has_no_history(self):
        history = play_rate_history({"p": set()}, team_weeks={1, 2}, as_of_week=1)
        self.assertIsNone(history["p"]["prior_play_rate"])
        self.assertEqual(history["p"]["prior_games_observed"], 0.0)

    def test_a_player_who_missed_everything_gets_zero_not_none(self):
        history = play_rate_history({"p": set()}, team_weeks={1, 2, 3}, as_of_week=4)
        self.assertEqual(history["p"]["prior_play_rate"], 0.0)


class TestUnfittedFallback(unittest.TestCase):
    """With no fit, predict falls back to the shrunk play rate -- never to 1.0.
    Assuming everyone plays is the behaviour this module exists to replace."""

    def test_no_history_falls_back_to_the_league_rate(self):
        model = AvailabilityModel().fit([])
        self.assertAlmostEqual(model.predict_one({}), LEAGUE_PLAY_RATE, places=6)

    def test_a_long_history_dominates_the_prior(self):
        model = AvailabilityModel().fit([])
        p = model.predict_one({"prior_play_rate": 0.2, "prior_games_observed": 40.0})
        self.assertLess(p, 0.35)

    def test_a_thin_history_is_pulled_toward_the_league_rate(self):
        model = AvailabilityModel().fit([])
        thin = model.predict_one({"prior_play_rate": 0.0, "prior_games_observed": 1.0})
        thick = model.predict_one({"prior_play_rate": 0.0, "prior_games_observed": 40.0})
        # 1 missed week is not evidence of a 0% availability rate.
        self.assertGreater(thin, thick)
        self.assertGreater(thin, 0.4)

    def test_too_few_rows_does_not_fit(self):
        model = AvailabilityModel().fit(rows(n_out=2, n_healthy=5))
        self.assertIsNone(model.beta)


class TestFittedModel(unittest.TestCase):
    def setUp(self):
        self.model = AvailabilityModel().fit(rows())

    def test_it_actually_fits(self):
        self.assertIsNotNone(self.model.beta)

    def test_out_is_driven_far_below_healthy(self):
        out = self.model.predict_one({
            "position": "WR", "report_status": "Out",
            "practice_status": "Did Not Participate In Practice",
            "prior_play_rate": 0.8, "prior_games_observed": 8.0,
        })
        healthy = self.model.predict_one({
            "position": "WR", "report_status": None,
            "practice_status": "Full Participation in Practice",
            "prior_play_rate": 0.9, "prior_games_observed": 8.0,
        })
        self.assertLess(out, 0.2)
        self.assertGreater(healthy, 0.8)

    def test_probabilities_are_valid(self):
        for row in ({}, {"report_status": "Out"}, {"prior_play_rate": 1e9}):
            p = self.model.predict_one(row)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_nan_designation_does_not_raise(self):
        # pandas hands back NaN, not None, for an empty cell -- and NaN is
        # truthy, so `value or ""` lets it through to .strip(). Guarded by
        # availability._text; this is the regression test for it.
        p = self.model.predict_one({
            "position": "WR", "report_status": float("nan"), "practice_status": float("nan"),
        })
        self.assertGreaterEqual(p, 0.0)

    def test_batch_predict_is_keyed_by_player_id(self):
        out = self.model.predict([
            {"player_id": "a", "position": "WR", "report_status": "Out"},
            {"player_id": "b", "position": "WR", "report_status": None},
        ])
        self.assertEqual(set(out), {"a", "b"})
        self.assertLess(out["a"], out["b"])

    def test_unlabelled_rows_are_ignored_when_fitting(self):
        AvailabilityModel().fit(rows() + [{"player_id": "x", "position": "WR"}])

    def test_dst_is_declared_always_available(self):
        # A team defence plays every week its team has a game; discounting it
        # would price a risk that does not exist.
        self.assertIn("DST", ALWAYS_AVAILABLE_POSITIONS)
        self.assertNotIn("K", ALWAYS_AVAILABLE_POSITIONS)


if __name__ == "__main__":
    unittest.main()
