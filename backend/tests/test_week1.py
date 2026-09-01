"""Unit tests for backend/week1.py.

Same discipline as tests/test_projections.py: everything here runs on synthetic
frames with no network. The nflreadpy adapters at the bottom of week1.py
(load_history_nflreadpy, load_week1_context_nflreadpy, load_player_meta_nflreadpy,
_load_depth_ranks) are deliberately NOT covered -- they need live data, and the
walk-forward validation that exercises them is documented in
docs/research/week1-cold-start-model.md.
"""

import math
import unittest

from week1 import (
    FEATURES,
    fit_interval_model,
    UNDRAFTED_PICK,
    UNDRAFTED_ROUND,
    Z_CLIP,
    Week1Model,
    _median,
    _num,
    _ridge,
    _standardize,
    build_feature_rows,
    prior_season_dvp,
    training_rows_from_history,
)


def _game(player_id, season, week, points, team="AAA", position="WR", **extra):
    return {
        "player_id": player_id, "season": season, "week": week, "points": points,
        "team": team, "position": position, "opponent_team": extra.pop("opponent_team", "BBB"),
        **extra,
    }


def _meta(position="WR", team="AAA", **extra):
    base = {
        "position": position, "team": team, "age": 26, "years_exp": 4,
        "draft_pick": 40, "draft_round": 2, "rookie_year": 2020, "depth_team": 1,
    }
    base.update(extra)
    return base


CONTEXT = {
    "AAA": {"opponent": "BBB", "is_home": 1.0, "implied_team_total": 24.5,
            "total_line": 47.0, "spread_line": 2.0},
    "BBB": {"opponent": "AAA", "is_home": 0.0, "implied_team_total": 22.5,
            "total_line": 47.0, "spread_line": -2.0},
}


class TestNumericCoercion(unittest.TestCase):
    """_num is the gate everything else relies on: pandas/polars hand back NaN
    rather than None for a missing numeric, and `float('nan') is not None`
    would otherwise let a NaN reach the linear algebra."""

    def test_nan_and_inf_are_treated_as_missing(self):
        self.assertIsNone(_num(float("nan")))
        self.assertIsNone(_num(float("inf")))
        self.assertIsNone(_num(None))
        self.assertIsNone(_num("not a number"))

    def test_real_numbers_pass_through(self):
        self.assertEqual(_num(3), 3.0)
        self.assertEqual(_num("2.5"), 2.5)
        self.assertEqual(_num(0), 0.0)  # zero is data, not missing

    def test_median_of_empty_is_none_not_zero(self):
        self.assertIsNone(_median([]))
        self.assertEqual(_median([1.0, 3.0]), 2.0)
        self.assertEqual(_median([1.0, 5.0, 3.0]), 3.0)


class TestRidge(unittest.TestCase):
    def test_recovers_a_known_linear_relationship(self):
        # y = 2 + 3*x1 - 1*x2, no noise. With a small penalty the fit should be
        # close to the truth; ridge biases coefficients toward zero, so this
        # asserts closeness rather than equality.
        design = [[x1, x2] for x1 in range(-3, 4) for x2 in range(-3, 4)]
        targets = [2 + 3 * a - b for a, b in design]
        beta = _ridge(design, targets, alpha=0.001)
        self.assertAlmostEqual(beta[0], 2.0, places=2)
        self.assertAlmostEqual(beta[1], 3.0, places=2)
        self.assertAlmostEqual(beta[2], -1.0, places=2)

    def test_intercept_is_not_penalised(self):
        # A constant target with zero-centred features: a penalised intercept
        # would be dragged below the true level. This is the reason _ridge
        # skips index 0 when adding alpha to the diagonal.
        design = [[-1.0], [0.0], [1.0]]
        targets = [10.0, 10.0, 10.0]
        beta = _ridge(design, targets, alpha=500.0)
        self.assertAlmostEqual(beta[0], 10.0, places=6)

    def test_handles_perfectly_collinear_features(self):
        # prior_receptions and prior_targets really are near-collinear in the
        # live feature set; the penalty is what keeps the normal equations
        # solvable. Without alpha this system is singular.
        design = [[x, 2 * x] for x in range(-5, 6)]
        targets = [3.0 * x for x in range(-5, 6)]
        beta = _ridge(design, targets, alpha=1.0)
        self.assertTrue(all(math.isfinite(b) for b in beta))


class TestStandardize(unittest.TestCase):
    def test_clip_bounds_extreme_inputs(self):
        z = _standardize([100.0], [0.0], [1.0], clip=Z_CLIP)
        self.assertEqual(z[0], Z_CLIP)
        z = _standardize([-100.0], [0.0], [1.0], clip=Z_CLIP)
        self.assertEqual(z[0], -Z_CLIP)

    def test_no_clip_by_default(self):
        self.assertEqual(_standardize([100.0], [0.0], [1.0])[0], 100.0)


class TestFeatureAssembly(unittest.TestCase):
    def setUp(self):
        self.prior = [
            _game("p1", 2023, w, points=10.0 + w, targets=6, receptions=4,
                  receiving_yards=60, target_share=0.2, wopr=0.4)
            for w in range(1, 11)
        ]

    def test_builds_one_row_per_rostered_player(self):
        rows = build_feature_rows(2024, self.prior, CONTEXT, {"p1": _meta(), "p2": _meta(team="BBB")})
        self.assertEqual({r["player_id"] for r in rows}, {"p1", "p2"})

    def test_prior_season_aggregates_are_means_not_totals(self):
        row = build_feature_rows(2024, self.prior, CONTEXT, {"p1": _meta()})[0]
        self.assertAlmostEqual(row["prior_ppg"], sum(10.0 + w for w in range(1, 11)) / 10)
        self.assertAlmostEqual(row["prior_targets"], 6.0)
        self.assertEqual(row["prior_games"], 10.0)

    def test_last8_window_uses_the_most_recent_games(self):
        row = build_feature_rows(2024, self.prior, CONTEXT, {"p1": _meta()})[0]
        self.assertAlmostEqual(row["prior_last8_ppg"], sum(10.0 + w for w in range(3, 11)) / 8)
        self.assertGreater(row["prior_last8_ppg"], row["prior_ppg"])

    def test_game_order_does_not_depend_on_input_order(self):
        shuffled = list(reversed(self.prior))
        a = build_feature_rows(2024, self.prior, CONTEXT, {"p1": _meta()})[0]
        b = build_feature_rows(2024, shuffled, CONTEXT, {"p1": _meta()})[0]
        self.assertAlmostEqual(a["prior_last8_ppg"], b["prior_last8_ppg"])

    def test_player_with_no_prior_season_gets_none_not_zero(self):
        # "Did not play last season" (a rookie) and "played and produced
        # nothing" are different states. Zero-filling would assert the second.
        row = build_feature_rows(2024, [], CONTEXT, {"rookie": _meta(rookie_year=2024)})[0]
        self.assertIsNone(row["prior_ppg"])
        self.assertIsNone(row["prior_targets"])
        self.assertEqual(row["is_rookie"], 1.0)

    def test_undrafted_player_gets_the_far_end_of_the_scale(self):
        row = build_feature_rows(
            2024, [], CONTEXT, {"udfa": _meta(draft_pick=None, draft_round=None)}
        )[0]
        self.assertEqual(row["draft_pick"], UNDRAFTED_PICK)
        self.assertEqual(row["draft_round"], UNDRAFTED_ROUND)

    def test_team_change_flag(self):
        stayed = build_feature_rows(2024, self.prior, CONTEXT, {"p1": _meta(team="AAA")})[0]
        moved = build_feature_rows(2024, self.prior, CONTEXT, {"p1": _meta(team="BBB")})[0]
        self.assertEqual(stayed["team_change"], 0.0)
        self.assertEqual(moved["team_change"], 1.0)

    def test_game_context_is_attached_from_the_players_own_team(self):
        rows = {r["player_id"]: r for r in build_feature_rows(
            2024, self.prior, CONTEXT, {"home": _meta(team="AAA"), "away": _meta(team="BBB")})}
        self.assertEqual(rows["home"]["implied_team_total"], 24.5)
        self.assertEqual(rows["home"]["is_home"], 1.0)
        self.assertEqual(rows["away"]["implied_team_total"], 22.5)
        self.assertEqual(rows["away"]["opponent"], "AAA")

    def test_availability_is_games_played_over_team_games(self):
        # p1 played 10 of the 12 weeks their team appears in the log.
        prior = self.prior + [_game("teammate", 2023, w, 5.0) for w in range(1, 13)]
        row = build_feature_rows(2024, prior, CONTEXT, {"p1": _meta()})[0]
        self.assertAlmostEqual(row["prior_availability"], 10 / 12)

    def test_players_without_a_team_or_position_are_skipped(self):
        rows = build_feature_rows(2024, self.prior, CONTEXT, {"x": {"position": None, "team": "AAA"}})
        self.assertEqual(rows, [])


class TestTrainingRows(unittest.TestCase):
    def test_only_labels_players_who_actually_played_week_1(self):
        games = [_game("p1", 2023, w, 10.0) for w in range(1, 11)]
        games += [_game("p1", 2024, 1, 18.0), _game("p2", 2023, 1, 5.0)]
        rows = training_rows_from_history(
            [2024], games, {2024: CONTEXT}, {2024: {"p1": _meta(), "p2": _meta()}})
        self.assertEqual([r["player_id"] for r in rows], ["p1"])
        self.assertEqual(rows[0]["actual_points"], 18.0)

    def test_features_never_read_the_target_season(self):
        # The as-of guarantee: a monstrous 2024 week-1 line must not leak into
        # the 2024 features, which may only see 2023 and 2022.
        games = [_game("p1", 2023, w, 4.0) for w in range(1, 11)]
        games += [_game("p1", 2024, 1, 99.0)]
        rows = training_rows_from_history([2024], games, {2024: CONTEXT}, {2024: {"p1": _meta()}})
        self.assertAlmostEqual(rows[0]["prior_ppg"], 4.0)
        self.assertAlmostEqual(rows[0]["prior_last8_ppg"], 4.0)


class TestWeek1Model(unittest.TestCase):
    def _training_rows(self, n=120):
        """Synthetic rows where week-1 points are a clean linear function of
        prior_ppg, so the fit has something real to recover."""
        rows = []
        for i in range(n):
            ppg = 2.0 + (i % 20)
            rows.append({
                "player_id": f"p{i}", "position": "WR", "season": 2024,
                "prior_ppg": ppg, "prior_targets": ppg / 2, "implied_team_total": 24.0,
                "draft_pick": 50.0, "actual_points": 1.5 * ppg,
            })
        return rows

    def test_learns_the_relationship_and_ranks_correctly(self):
        model = Week1Model().fit(self._training_rows())
        low = model.predict_one({"position": "WR", "season": 2025, "prior_ppg": 3.0})
        high = model.predict_one({"position": "WR", "season": 2025, "prior_ppg": 20.0})
        self.assertGreater(high["projected_points"], low["projected_points"])

    def test_output_contract_matches_project_player(self):
        model = Week1Model().fit(self._training_rows())
        out = model.predict_one({"position": "WR", "season": 2025, "prior_ppg": 10.0})
        for key in ("source", "season", "week", "projected_points", "confidence", "games_used"):
            self.assertIn(key, out)
        self.assertEqual(out["source"], "week1_model")
        self.assertEqual(out["week"], 1)
        self.assertEqual(out["games_used"], 0)
        self.assertIsInstance(out["projected_points"], float)

    def test_projection_is_never_negative(self):
        model = Week1Model().fit(self._training_rows())
        out = model.predict_one({"position": "WR", "season": 2025, "prior_ppg": -500.0})
        self.assertGreaterEqual(out["projected_points"], 0.0)

    def test_projection_is_capped_at_the_training_ceiling(self):
        # Regression guard for the 175.8-point TE: an out-of-range input used
        # to extrapolate without bound and, on its own, tripled a season's RMSE.
        rows = self._training_rows()
        ceiling = max(r["actual_points"] for r in rows)
        model = Week1Model().fit(rows)
        out = model.predict_one({
            "position": "WR", "season": 2025, "prior_ppg": 1e9,
            "prior_targets": 1e9, "draft_pick": 1e9,
        })
        self.assertLessEqual(out["projected_points"], ceiling)
        self.assertTrue(math.isfinite(out["projected_points"]))

    def test_unknown_position_degrades_instead_of_raising(self):
        model = Week1Model().fit(self._training_rows())
        out = model.predict_one({"position": "K", "season": 2025})
        self.assertEqual(out["projected_points"], 0.0)
        self.assertEqual(out["confidence"], "no_model")

    def test_thin_position_falls_back_to_its_mean(self):
        rows = self._training_rows() + [
            {"player_id": "q1", "position": "QB", "season": 2024, "prior_ppg": 20.0, "actual_points": 18.0},
            {"player_id": "q2", "position": "QB", "season": 2024, "prior_ppg": 22.0, "actual_points": 22.0},
        ]
        model = Week1Model().fit(rows)
        out = model.predict_one({"position": "QB", "season": 2025, "prior_ppg": 30.0})
        self.assertEqual(out["confidence"], "positional_fallback")
        self.assertAlmostEqual(out["projected_points"], 20.0)

    def test_confidence_reflects_how_much_is_actually_known(self):
        model = Week1Model().fit(self._training_rows())
        known = {f: 1.0 for f in FEATURES}
        known.update(position="WR", season=2025)
        self.assertEqual(model.predict_one(known)["confidence"], "full")
        self.assertEqual(model.predict_one({"position": "WR", "season": 2025})["confidence"], "thin")

    def test_feature_coverage_is_reported(self):
        model = Week1Model().fit(self._training_rows())
        out = model.predict_one({"position": "WR", "season": 2025, "prior_ppg": 10.0})
        self.assertAlmostEqual(out["feature_coverage"], 1 / len(FEATURES), places=3)

    def test_missing_features_do_not_raise(self):
        model = Week1Model().fit(self._training_rows())
        out = model.predict_one({"position": "WR", "season": 2025})
        self.assertTrue(math.isfinite(out["projected_points"]))

    def test_nan_features_behave_exactly_like_absent_ones(self):
        model = Week1Model().fit(self._training_rows())
        absent = model.predict_one({"position": "WR", "season": 2025})
        nan = model.predict_one({"position": "WR", "season": 2025, "prior_ppg": float("nan")})
        self.assertAlmostEqual(absent["projected_points"], nan["projected_points"])

    def test_batch_predict_is_keyed_by_player_id(self):
        model = Week1Model().fit(self._training_rows())
        out = model.predict([
            {"player_id": "a", "position": "WR", "season": 2025, "prior_ppg": 5.0},
            {"player_id": "b", "position": "WR", "season": 2025, "prior_ppg": 15.0},
        ])
        self.assertEqual(set(out), {"a", "b"})
        self.assertGreater(out["b"]["projected_points"], out["a"]["projected_points"])

    def test_fit_ignores_rows_without_a_label(self):
        rows = self._training_rows()
        rows.append({"player_id": "x", "position": "WR", "season": 2024, "prior_ppg": 9.0})
        Week1Model().fit(rows)  # must not raise on the unlabelled row


class TestFitIntervalModel(unittest.TestCase):
    """Week-1 intervals must come from the week-1 model's own HELD-OUT
    residuals -- the in-season band belongs to a different estimator, and
    in-sample residuals would make the interval look tighter than it is."""

    def _rows(self, seasons=(2020, 2021, 2022, 2023)):
        rows = []
        for season in seasons:
            for i in range(120):
                ppg = 2.0 + (i % 15)
                rows.append({
                    "player_id": f"p{season}_{i}", "season": season, "position": "WR",
                    "prior_ppg": ppg, "prior_targets": ppg / 2,
                    "actual_points": 1.2 * ppg + ((i * 7) % 9 - 4),
                })
        return rows

    def test_returns_none_without_enough_seasons_to_hold_out(self):
        # One season cannot be split into fit-and-hold-out, so there is nothing
        # honest to measure residuals on. min_seasons defaults to 1, so two
        # seasons IS enough (fit on the first, hold out the second).
        self.assertIsNone(fit_interval_model(self._rows(seasons=(2022,))))
        self.assertIsNone(
            fit_interval_model(self._rows(seasons=(2022, 2023)), min_seasons=2)
        )

    def test_two_seasons_is_enough_at_the_default(self):
        self.assertIsNotNone(fit_interval_model(self._rows(seasons=(2022, 2023))))

    def test_fits_once_a_season_can_be_held_out(self):
        model = fit_interval_model(self._rows())
        self.assertIsNotNone(model)
        self.assertIsNotNone(model.interval("WR", 10.0))

    def test_interval_brackets_the_projection(self):
        model = fit_interval_model(self._rows())
        low, high = model.interval("WR", 10.0)
        self.assertLess(low, 10.0)
        self.assertGreater(high, 10.0)
        self.assertGreaterEqual(low, 0.0)

    def test_empty_input_returns_none(self):
        self.assertIsNone(fit_interval_model([]))


class TestPriorSeasonDvp(unittest.TestCase):
    def test_rate_is_relative_to_league_average_and_regressed_halfway(self):
        # BBB allows 20/game to WRs, CCC allows 10, league average 15.
        games = [_game(f"a{w}", 2023, w, 20.0, opponent_team="BBB") for w in range(1, 5)]
        games += [_game(f"b{w}", 2023, w, 10.0, opponent_team="CCC") for w in range(1, 5)]
        dvp = prior_season_dvp(games, 2023)
        # raw ratio 20/15 = 1.333 -> regressed halfway to 1.0 = 1.167
        self.assertAlmostEqual(dvp[("BBB", "WR")], 0.5 * (20 / 15) + 0.5, places=3)
        self.assertAlmostEqual(dvp[("CCC", "WR")], 0.5 * (10 / 15) + 0.5, places=3)
        self.assertGreater(dvp[("BBB", "WR")], dvp[("CCC", "WR")])

    def test_points_allowed_are_summed_within_a_week_then_averaged(self):
        # Two WRs in the same game against BBB: that is one 30-point week
        # allowed, not two 15-point observations.
        games = [_game("a", 2023, 1, 20.0, opponent_team="BBB"),
                 _game("b", 2023, 1, 10.0, opponent_team="BBB"),
                 _game("c", 2023, 1, 30.0, opponent_team="CCC")]
        dvp = prior_season_dvp(games, 2023)
        self.assertAlmostEqual(dvp[("BBB", "WR")], dvp[("CCC", "WR")])

    def test_other_seasons_are_ignored(self):
        games = [_game("a", 2022, 1, 99.0, opponent_team="BBB"),
                 _game("b", 2023, 1, 15.0, opponent_team="BBB")]
        self.assertAlmostEqual(prior_season_dvp(games, 2023)[("BBB", "WR")], 1.0)


if __name__ == "__main__":
    unittest.main()
