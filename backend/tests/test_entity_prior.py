"""Unit tests for entity_prior.py's pure half (everything except
load_prior_season_logs_nflreadpy, which needs the network -- same split as
every other module in this backend).

What these are guarding. The defect this module fixes was not a crash: the
in-house tier silently collapsed toward one number per position in the early
weeks, and the only symptom was a tight end with a 95% snap share ranking
126th of 209. So the tests that matter here are the ones asserting SPREAD is
preserved and that the scope rule holds -- a regression would otherwise look
like perfectly valid output again.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entity_prior import (  # noqa: E402
    COVERED_POSITIONS,
    DEFAULT_PRIOR_K,
    FIRST_COVERED_WEEK,
    applies,
    entity_baselines,
    entity_season_means,
)

CONFIG = {"linear": {"rec_yd": 0.1, "rec_td": 6, "reception": 1}}


def game(week, position="WR", rec_yd=0, rec_td=0, reception=0):
    return {
        "season": 2025, "week": week, "position": position,
        "rec_yd": rec_yd, "rec_td": rec_td, "reception": reception,
    }


class EntitySeasonMeansTest(unittest.TestCase):
    def test_mean_and_games_are_scored_through_the_config(self):
        logs = {"wr1": [game(1, rec_yd=100), game(2, rec_yd=50)]}
        out = entity_season_means(logs, CONFIG)
        self.assertEqual(out["wr1"][1], 2)
        self.assertAlmostEqual(out["wr1"][0], 7.5)   # (10.0 + 5.0) / 2
        self.assertEqual(out["wr1"][2], "WR")

    def test_uncovered_positions_are_excluded(self):
        logs = {"k1": [game(1, position="K", rec_yd=10)],
                "wr1": [game(1, rec_yd=10)]}
        out = entity_season_means(logs, CONFIG)
        self.assertNotIn("k1", out)
        self.assertIn("wr1", out)

    def test_a_single_game_prior_is_kept_not_filtered(self):
        """No minimum-games filter by design: shrink() already weights a
        one-game prior at 1/(1+k). Filtering would be a second, redundant
        mechanism -- and every measurement behind this module was taken
        without one."""
        out = entity_season_means({"wr1": [game(1, rec_yd=200)]}, CONFIG)
        self.assertEqual(out["wr1"][1], 1)

    def test_empty_log_is_skipped_rather_than_a_zero_division(self):
        self.assertEqual(entity_season_means({"wr1": []}, CONFIG), {})


class EntityBaselinesTest(unittest.TestCase):
    def test_more_prior_games_pulls_closer_to_the_players_own_mean(self):
        """The whole point: a player with a full prior season should land near
        his own average, not the pool's."""
        thin = entity_baselines({"a": (20.0, 1, "WR")}, {"WR": 5.0}, k=2.0)
        thick = entity_baselines({"a": (20.0, 16, "WR")}, {"WR": 5.0}, k=2.0)
        self.assertLess(thin["a"], thick["a"])
        self.assertAlmostEqual(thin["a"], (1 * 20.0 + 2 * 5.0) / 3, places=3)
        self.assertAlmostEqual(thick["a"], (16 * 20.0 + 2 * 5.0) / 18, places=3)

    def test_targets_keep_the_spread_the_positional_mean_destroys(self):
        """The regression guard for the actual defect. Three players with very
        different prior seasons must NOT come out at one number."""
        means = {"a": (18.0, 16, "TE"), "b": (9.0, 16, "TE"), "c": (2.0, 16, "TE")}
        out = entity_baselines(means, {"TE": 4.0})
        self.assertEqual(len(set(out.values())), 3)
        self.assertGreater(max(out.values()) - min(out.values()), 10.0)

    def test_player_whose_position_has_no_baseline_is_omitted(self):
        """Omitted, not given a bare prior-season mean -- the caller then falls
        back to its own positional baseline, which is the old behaviour."""
        out = entity_baselines({"a": (10.0, 16, "WR")}, {"TE": 4.0})
        self.assertEqual(out, {})

    def test_a_none_baseline_is_treated_as_absent(self):
        out = entity_baselines({"a": (10.0, 16, "WR")}, {"WR": None})
        self.assertEqual(out, {})

    def test_default_k_is_the_value_that_was_measured(self):
        """DEFAULT_PRIOR_K is not a free knob: every number in the module
        docstring was measured at it, and a per-position fit was rejected as
        overfitting (the optimum moved 6x between adjacent seasons)."""
        self.assertEqual(DEFAULT_PRIOR_K, 2.0)
        explicit = entity_baselines({"a": (10.0, 4, "WR")}, {"WR": 5.0}, k=DEFAULT_PRIOR_K)
        default = entity_baselines({"a": (10.0, 4, "WR")}, {"WR": 5.0})
        self.assertEqual(explicit, default)


class ScopeTest(unittest.TestCase):
    def test_week_1_is_not_covered(self):
        """Week 1 has better-informed models already shipped -- week1.py for
        skill positions, week1_kdst.py for K and D/ST."""
        self.assertFalse(applies(1, "WR"))
        self.assertTrue(applies(FIRST_COVERED_WEEK, "WR"))

    def test_kickers_and_defences_are_not_covered(self):
        """Plausible by analogy, unmeasured in this work. CLAUDE.md's rule is
        that a result does not transfer to a population it was not measured
        on."""
        self.assertFalse(applies(5, "K"))
        self.assertFalse(applies(5, "DST"))
        for position in COVERED_POSITIONS:
            self.assertTrue(applies(5, position))

    def test_unknown_position_is_not_covered(self):
        self.assertFalse(applies(5, None))
        self.assertFalse(applies(5, "OL"))


class SharedShrinkTest(unittest.TestCase):
    def test_shrink_is_week1_kdsts_so_the_two_cannot_drift(self):
        import week1_kdst

        import entity_prior

        self.assertIs(entity_prior.shrink, week1_kdst.shrink)

    def test_zero_prior_games_returns_the_positional_mean_exactly(self):
        import entity_prior

        self.assertEqual(entity_prior.shrink(99.0, 0, 5.0, 2.0), 5.0)


if __name__ == "__main__":
    unittest.main()
