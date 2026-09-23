"""Unit tests for backend/availability.py.

The nflreadpy adapters (load_injury_report_nflreadpy,
build_training_rows_nflreadpy) are NOT covered -- they need the network. Their
real-data validation is in docs/research/scoring-engine-and-model-audit.md.
"""

import unittest

from availability import (
    ALWAYS_AVAILABLE_POSITIONS,
    LEAGUE_PLAY_RATE,
    REPORT_STATUSES,
    AvailabilityModel,
    _SLEEPER_TO_REPORT_STATUS,
    expected_points,
    load_injury_report_sleeper,
    merge_injury_reports,
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


class TestDepthChartPrior(unittest.TestCase):
    """Depth rank enters as position-BY-depth indicators, because the effect is
    neither uniform across positions nor monotonic within them: measured over
    2018-24 on undesignated players, a backup QB plays 0.49 of the time and a
    backup RB 0.88. One linear slope would average those into a number wrong
    for both."""

    def _rows(self, n=140):
        out = []
        for i in range(n):
            # QB1 nearly always plays; QB2 rarely; RB2 usually does.
            for pos, rank, played in (
                ("QB", 1, 1), ("QB", 2, 0), ("RB", 1, 1), ("RB", 2, 1),
            ):
                out.append({
                    "player_id": f"{pos}{rank}_{i}", "position": pos, "played": played,
                    "report_status": None, "practice_status": None,
                    "prior_play_rate": 0.8, "prior_games_observed": 8.0,
                    "depth_rank": rank,
                })
        return out

    def test_starters_and_backups_separate(self):
        model = AvailabilityModel().fit(self._rows())
        base = {"report_status": None, "prior_play_rate": 0.8, "prior_games_observed": 8.0}
        qb1 = model.predict_one({**base, "position": "QB", "depth_rank": 1})
        qb2 = model.predict_one({**base, "position": "QB", "depth_rank": 2})
        self.assertGreater(qb1, 0.8)
        self.assertLess(qb2, 0.4)

    def test_the_same_depth_means_different_things_by_position(self):
        # The whole reason for the interaction: a backup RB is not a backup QB.
        model = AvailabilityModel().fit(self._rows())
        base = {"report_status": None, "prior_play_rate": 0.8, "prior_games_observed": 8.0}
        qb2 = model.predict_one({**base, "position": "QB", "depth_rank": 2})
        rb2 = model.predict_one({**base, "position": "RB", "depth_rank": 2})
        self.assertGreater(rb2, qb2 + 0.3)

    def test_rank_beyond_third_string_is_capped_not_extrapolated(self):
        model = AvailabilityModel().fit(self._rows())
        base = {"report_status": None, "position": "QB",
                "prior_play_rate": 0.8, "prior_games_observed": 8.0}
        self.assertAlmostEqual(
            model.predict_one({**base, "depth_rank": 3}),
            model.predict_one({**base, "depth_rank": 9}),
            places=6,
        )

    def test_missing_rank_is_its_own_state_not_an_average_depth(self):
        # ~8% of player-weeks have no chart entry; treating that as "average
        # depth" would invent information.
        model = AvailabilityModel().fit(self._rows())
        base = {"report_status": None, "position": "QB",
                "prior_play_rate": 0.8, "prior_games_observed": 8.0}
        unknown = model.predict_one(base)
        self.assertGreaterEqual(unknown, 0.0)
        self.assertLessEqual(unknown, 1.0)
        self.assertNotAlmostEqual(unknown, model.predict_one({**base, "depth_rank": 1}), places=3)

    def test_a_junk_rank_is_treated_as_unknown(self):
        model = AvailabilityModel().fit(self._rows())
        base = {"report_status": None, "position": "QB",
                "prior_play_rate": 0.8, "prior_games_observed": 8.0}
        for junk in ("x", None, float("nan"), 0, -2):
            p = model.predict_one({**base, "depth_rank": junk})
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


class TestSleeperInjuryReport(unittest.TestCase):
    """Sleeper is the only LIVE injury source for a season nflreadpy has not
    published yet, which is every pre-season and week-1 report."""

    def setUp(self):
        import news
        self._real = news.fetch_sleeper_injury_status
        news.fetch_sleeper_injury_status = lambda: {
            "12345": {"designation": "Out", "riskLevel": "high", "body_part": "Knee"},
            "jane doe": {"designation": "Questionable", "riskLevel": "low", "body_part": None},
            "ir player": {"designation": "IR", "riskLevel": "high", "body_part": "Achilles"},
            "unmapped player": {"designation": "Healthy", "riskLevel": "none", "body_part": None},
        }

    def tearDown(self):
        import news
        news.fetch_sleeper_injury_status = self._real

    def test_matches_on_espn_id_first(self):
        out = load_injury_report_sleeper([{"playerId": "p1", "name": "Someone Else", "espnId": 12345}])
        self.assertEqual(out["p1"]["report_status"], "Out")

    def test_falls_back_to_lowercased_name(self):
        out = load_injury_report_sleeper([{"playerId": "p2", "name": "Jane Doe"}])
        self.assertEqual(out["p2"]["report_status"], "Questionable")

    def test_ir_collapses_to_out(self):
        # Both mean unavailable this week, and "Out" is the level the model has
        # training data for. A separate IR level would extrapolate a
        # coefficient from nothing, for an answer that is already ~0.
        out = load_injury_report_sleeper([{"playerId": "p3", "name": "IR Player"}])
        self.assertEqual(out["p3"]["report_status"], "Out")

    def test_practice_status_is_absent_not_invented(self):
        # Sleeper publishes a designation only. Inventing a practice status
        # would fabricate the feature that splits Questionable 0.51 -> 0.79.
        out = load_injury_report_sleeper([{"playerId": "p2", "name": "Jane Doe"}])
        self.assertIsNone(out["p2"]["practice_status"])

    def test_healthy_and_unknown_players_are_omitted(self):
        out = load_injury_report_sleeper([
            {"playerId": "p4", "name": "Unmapped Player"},   # designation not in the map
            {"playerId": "p5", "name": "Nobody At All"},     # absent from Sleeper
        ])
        self.assertEqual(out, {})

    def test_every_mapped_status_is_one_the_model_knows(self):
        for status in _SLEEPER_TO_REPORT_STATUS.values():
            self.assertIn(status, REPORT_STATUSES)

    def test_a_failing_feed_degrades_to_empty(self):
        import news
        def boom():
            raise RuntimeError("network down")
        news.fetch_sleeper_injury_status = boom
        self.assertEqual(load_injury_report_sleeper([{"playerId": "p", "name": "x"}]), {})


class TestMergeInjuryReports(unittest.TestCase):
    """Mid-week nflreadpy covers only the teams that have filed. On 2026-09-23
    that was 22 rows, all ATL/GB, and letting it win outright discarded ESPN's
    800 -- every Out player on the other 30 teams lost his designation."""

    DNP = "Did Not Participate In Practice"

    def test_partial_nflreadpy_report_keeps_espn_designations_for_everyone_else(self):
        nflreadpy_report = {"atl1": {"report_status": None, "practice_status": self.DNP}}
        espn = {
            "nyg_dart": {"report_status": "Out", "practice_status": None},
            "was_daniels": {"report_status": "Out", "practice_status": None},
        }
        merged, source = merge_injury_reports(nflreadpy_report, espn, {})
        self.assertEqual(merged["nyg_dart"]["report_status"], "Out")
        self.assertEqual(merged["was_daniels"]["report_status"], "Out")
        self.assertEqual(merged["atl1"]["practice_status"], self.DNP)
        self.assertIn("nflreadpy 1", source)
        self.assertIn("espn 2", source)

    def test_nflreadpy_adds_practice_without_erasing_a_game_status(self):
        # Mid-week rows carry practice but no game status yet.
        nflreadpy_report = {"p": {"report_status": None, "practice_status": self.DNP}}
        espn = {"p": {"report_status": "Out", "practice_status": None}}
        merged, _ = merge_injury_reports(nflreadpy_report, espn, {})
        self.assertEqual(merged["p"], {"report_status": "Out", "practice_status": self.DNP})

    def test_nflreadpy_game_status_wins_when_it_has_one(self):
        nflreadpy_report = {"p": {"report_status": "Questionable", "practice_status": "Full Participation in Practice"}}
        espn = {"p": {"report_status": "Out", "practice_status": None}}
        merged, _ = merge_injury_reports(nflreadpy_report, espn, {})
        self.assertEqual(merged["p"]["report_status"], "Questionable")

    def test_espn_wins_over_sleeper_and_sleeper_fills_gaps(self):
        espn = {"a": {"report_status": "Doubtful", "practice_status": None}}
        sleeper = {
            "a": {"report_status": "Questionable", "practice_status": None},
            "b": {"report_status": "Out", "practice_status": None},
        }
        merged, _ = merge_injury_reports({}, espn, sleeper)
        self.assertEqual(merged["a"]["report_status"], "Doubtful")
        self.assertEqual(merged["b"]["report_status"], "Out")

    def test_inputs_are_not_mutated(self):
        espn = {"p": {"report_status": "Out", "practice_status": None}}
        merge_injury_reports({"p": {"report_status": None, "practice_status": self.DNP}}, espn, {})
        self.assertIsNone(espn["p"]["practice_status"])

    def test_no_sources(self):
        self.assertEqual(merge_injury_reports({}, {}, {}), ({}, "none"))


if __name__ == "__main__":
    unittest.main()
