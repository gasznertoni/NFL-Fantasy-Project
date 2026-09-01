"""Unit tests for backend/blend.py -- the in-season volume + context ridge."""

import math
import unittest

from blend import (
    BASE_COLUMNS,
    CONTEXT_COLUMNS,
    FEATURES,
    VOLUME_COLUMNS,
    BlendModel,
    build_feature_row,
    rolling_volume,
)


def game(season, week, points, **stats):
    return {"season": season, "week": week, "points": points, **stats}


class TestRollingVolume(unittest.TestCase):
    def setUp(self):
        self.log = [game(2025, w, 10.0, targets=w, carries=2 * w) for w in range(1, 8)]

    def test_only_strictly_prior_games_count(self):
        out = rolling_volume(self.log, 2025, week=4, window=8, decay=1.0)
        # weeks 1-3 -> targets 1,2,3 -> mean 2.0
        self.assertAlmostEqual(out["targets"], 2.0)

    def test_window_truncates_to_the_trailing_games(self):
        out = rolling_volume(self.log, 2025, week=8, window=3, decay=1.0)
        # weeks 5,6,7 -> targets 5,6,7 -> mean 6.0
        self.assertAlmostEqual(out["targets"], 6.0)

    def test_decay_leans_on_recent_games(self):
        flat = rolling_volume(self.log, 2025, week=8, window=7, decay=1.0)
        decayed = rolling_volume(self.log, 2025, week=8, window=7, decay=0.5)
        self.assertGreater(decayed["targets"], flat["targets"])

    def test_other_seasons_are_excluded(self):
        log = self.log + [game(2024, 17, 99.0, targets=999)]
        out = rolling_volume(log, 2025, week=4, window=8, decay=1.0)
        self.assertAlmostEqual(out["targets"], 2.0)

    def test_no_prior_games_returns_empty(self):
        self.assertEqual(rolling_volume(self.log, 2025, week=1, window=8), {})

    def test_a_column_absent_everywhere_stays_absent(self):
        # A QB has no target share. Emitting 0.0 would tell the fit a real zero
        # was observed; leaving it out lets the missing-indicator carry it.
        out = rolling_volume(self.log, 2025, week=4, window=8)
        self.assertNotIn("target_share", out)
        self.assertIn("targets", out)

    def test_a_column_present_in_only_some_games_averages_over_those(self):
        log = [
            game(2025, 1, 5.0, targets=4),
            game(2025, 2, 5.0),  # no targets recorded
            game(2025, 3, 5.0, targets=8),
        ]
        out = rolling_volume(log, 2025, week=4, window=8, decay=1.0)
        self.assertAlmostEqual(out["targets"], 6.0)

    def test_input_order_does_not_matter(self):
        a = rolling_volume(self.log, 2025, 5, 8, 0.9)
        b = rolling_volume(list(reversed(self.log)), 2025, 5, 8, 0.9)
        self.assertAlmostEqual(a["targets"], b["targets"])


class TestFeatureSet(unittest.TestCase):
    def test_features_is_the_union_of_its_blocks(self):
        self.assertEqual(FEATURES, BASE_COLUMNS + VOLUME_COLUMNS + CONTEXT_COLUMNS)

    def test_the_base_estimate_is_a_feature(self):
        # The blend REFINES projections.py rather than replacing it; if the
        # rolling average stopped being an input this would silently become a
        # different model.
        self.assertIn("shrunk_avg", BASE_COLUMNS)


class TestBuildFeatureRow(unittest.TestCase):
    def test_merges_projection_volume_and_context(self):
        row = build_feature_row(
            "p1", "WR",
            {"rolling_avg": 9.0, "shrunk_avg": 8.5, "games_used": 5},
            {"targets": 7.0},
            {"implied_team_total": 26.0, "total_line": 49.0, "spread_line": 3.0, "is_home": 1.0},
        )
        self.assertEqual(row["player_id"], "p1")
        self.assertEqual(row["shrunk_avg"], 8.5)
        self.assertEqual(row["targets"], 7.0)
        self.assertAlmostEqual(row["implied_team_total"], 26.0)

    def test_missing_context_falls_back_to_league_average(self):
        row = build_feature_row("p1", "WR", {"shrunk_avg": 8.5}, {}, None)
        self.assertIsInstance(row["implied_team_total"], float)


class TestBlendModel(unittest.TestCase):
    def _rows(self, n=600):
        """Actual points are a clean linear function of the base estimate plus
        volume, so the fit has real signal to recover."""
        out = []
        for i in range(n):
            base = 2.0 + (i % 15)
            targets = base / 2.0
            out.append({
                "player_id": f"p{i}", "position": "WR",
                "rolling_avg": base, "shrunk_avg": base, "games_used": 6,
                "targets": targets, "implied_team_total": 24.0,
                "actual_points": 1.2 * base + 0.5 * targets,
            })
        return out

    def test_unfitted_position_returns_the_base_estimate(self):
        model = BlendModel()
        out = model.predict_one({"position": "WR", "shrunk_avg": 9.0, "rolling_avg": 11.0})
        self.assertEqual(out, 9.0)

    def test_unfitted_falls_back_through_rolling_avg_then_zero(self):
        model = BlendModel()
        self.assertEqual(model.predict_one({"position": "WR", "rolling_avg": 11.0}), 11.0)
        self.assertEqual(model.predict_one({"position": "WR"}), 0.0)

    def test_thin_position_is_not_fitted(self):
        model = BlendModel().fit(self._rows(n=50))
        self.assertEqual(model.fitted_positions, ())

    def test_it_fits_and_ranks_correctly(self):
        model = BlendModel().fit(self._rows())
        self.assertEqual(model.fitted_positions, ("WR",))
        low = model.predict_one({"position": "WR", "shrunk_avg": 3.0, "rolling_avg": 3.0,
                                 "targets": 1.5, "games_used": 6, "implied_team_total": 24.0})
        high = model.predict_one({"position": "WR", "shrunk_avg": 15.0, "rolling_avg": 15.0,
                                  "targets": 7.5, "games_used": 6, "implied_team_total": 24.0})
        self.assertGreater(high, low)

    def test_never_negative_and_never_above_the_training_ceiling(self):
        rows = self._rows()
        ceiling = max(r["actual_points"] for r in rows)
        model = BlendModel().fit(rows)
        wild = model.predict_one({"position": "WR", "shrunk_avg": 1e9, "targets": 1e9,
                                  "rolling_avg": 1e9, "games_used": 6})
        self.assertLessEqual(wild, ceiling)
        self.assertGreaterEqual(
            model.predict_one({"position": "WR", "shrunk_avg": -1e9, "rolling_avg": -1e9}), 0.0
        )
        self.assertTrue(math.isfinite(wild))

    def test_missing_features_do_not_raise(self):
        model = BlendModel().fit(self._rows())
        self.assertTrue(math.isfinite(model.predict_one({"position": "WR", "shrunk_avg": 8.0})))

    def test_nan_features_behave_like_absent_ones(self):
        model = BlendModel().fit(self._rows())
        absent = model.predict_one({"position": "WR", "shrunk_avg": 8.0})
        nan = model.predict_one({"position": "WR", "shrunk_avg": 8.0, "targets": float("nan")})
        self.assertAlmostEqual(absent, nan)

    def test_batch_predict_is_keyed_by_player_id(self):
        model = BlendModel().fit(self._rows())
        out = model.predict([
            {"player_id": "a", "position": "WR", "shrunk_avg": 3.0, "targets": 1.5},
            {"player_id": "b", "position": "WR", "shrunk_avg": 15.0, "targets": 7.5},
        ])
        self.assertGreater(out["b"], out["a"])

    def test_unlabelled_rows_are_ignored(self):
        BlendModel().fit(self._rows() + [{"player_id": "x", "position": "WR", "shrunk_avg": 5.0}])


if __name__ == "__main__":
    unittest.main()
