"""Unit tests for backend/calibration.py -- variance components, the
rank-preserving affine correction, and the empirical interval model."""

import unittest

from calibration import (
    DEFAULT_SHRINKAGE_K,
    AFFINE_SLOPE_BOUNDS,
    IntervalModel,
    apply_affine,
    empirical_bayes_weight,
    fit_affine,
    shrinkage_k_by_position,
    variance_components,
)


def spread_players(n=60, games=10, between=4.0, within=1.0, seed=7):
    """Players whose true means are spread by `between` and whose weekly noise
    is `within`, generated deterministically so the recovered variance
    components are reproducible."""
    state = seed
    def rand():
        nonlocal state
        state = (1103515245 * state + 12345) % (2 ** 31)
        return state / (2 ** 31) - 0.5
    out = {}
    for i in range(n):
        level = 10.0 + between * (rand() * 2)
        out[f"p{i}"] = [level + within * (rand() * 2) for _ in range(games)]
    return out


class TestVarianceComponents(unittest.TestCase):
    def test_returns_none_below_the_minimum_population(self):
        self.assertIsNone(variance_components({"a": [1.0] * 10}))

    def test_recovers_the_direction_of_the_spread(self):
        wide = variance_components(spread_players(between=8.0, within=1.0))
        narrow = variance_components(spread_players(between=1.0, within=8.0))
        self.assertLess(wide["k"], narrow["k"])
        self.assertGreater(wide["icc"], narrow["icc"])

    def test_icc_is_a_proportion(self):
        components = variance_components(spread_players())
        self.assertGreater(components["icc"], 0.0)
        self.assertLess(components["icc"], 1.0)

    def test_short_player_seasons_are_excluded(self):
        pool = spread_players(n=40)
        pool["stub"] = [5.0, 6.0]  # 2 games: too few to estimate a variance from
        self.assertEqual(variance_components(pool)["n_players"], 40.0)

    def test_nan_points_are_dropped_not_propagated(self):
        pool = spread_players(n=40)
        pool["p0"] = pool["p0"] + [float("nan")]
        components = variance_components(pool)
        self.assertGreater(components["k"], 0.0)


class TestShrinkageKByPosition(unittest.TestCase):
    def test_falls_back_to_the_measured_defaults(self):
        out = shrinkage_k_by_position({})
        self.assertEqual(out, DEFAULT_SHRINKAGE_K)

    def test_a_position_with_data_overrides_its_default(self):
        out = shrinkage_k_by_position({"QB": spread_players(between=8.0, within=1.0)})
        self.assertNotAlmostEqual(out["QB"], DEFAULT_SHRINKAGE_K["QB"])
        self.assertEqual(out["RB"], DEFAULT_SHRINKAGE_K["RB"])  # untouched

    def test_every_roster_position_has_a_default(self):
        for position in ("QB", "RB", "WR", "TE", "DST", "K"):
            self.assertIn(position, DEFAULT_SHRINKAGE_K)


class TestEmpiricalBayesWeight(unittest.TestCase):
    def test_zero_games_earns_no_weight(self):
        self.assertEqual(empirical_bayes_weight(0, 1.5), 0.0)

    def test_weight_rises_with_sample_size_and_never_reaches_one(self):
        weights = [empirical_bayes_weight(n, 1.5) for n in (1, 3, 6, 12, 60)]
        self.assertEqual(weights, sorted(weights))
        self.assertLess(weights[-1], 1.0)

    def test_matches_the_closed_form(self):
        self.assertAlmostEqual(empirical_bayes_weight(6, 2.0), 6 / 8)

    def test_it_is_continuous_across_any_window_boundary(self):
        # The legacy rule jumped to 1.0 exactly at games_used == window. This
        # one has no cliff anywhere -- that discontinuity is the defect it
        # replaces.
        a = empirical_bayes_weight(7, 1.87)
        b = empirical_bayes_weight(8, 1.87)
        self.assertLess(abs(b - a), 0.05)

    def test_larger_k_shrinks_harder(self):
        self.assertLess(empirical_bayes_weight(6, 4.0), empirical_bayes_weight(6, 1.0))


class TestAffine(unittest.TestCase):
    def test_recovers_a_known_relationship(self):
        pairs = [(x, 2.0 + 0.8 * x) for x in range(300)]
        a, b = fit_affine(pairs)
        self.assertAlmostEqual(a, 2.0, places=4)
        self.assertAlmostEqual(b, 0.8, places=4)

    def test_too_few_rows_returns_none(self):
        self.assertIsNone(fit_affine([(1.0, 1.0), (2.0, 2.0)]))

    def test_a_degenerate_slope_is_rejected(self):
        # Callers treat None as "apply the identity", so a broken fit degrades
        # to today's behaviour rather than to a wrong number.
        pairs = [(x, 100.0 * x) for x in range(300)]
        self.assertIsNone(fit_affine(pairs))
        self.assertGreater(100.0, AFFINE_SLOPE_BOUNDS[1])

    def test_constant_projection_returns_none(self):
        self.assertIsNone(fit_affine([(5.0, float(i)) for i in range(300)]))

    def test_nan_pairs_are_skipped(self):
        pairs = [(x, 2.0 + 0.8 * x) for x in range(300)] + [(float("nan"), 5.0)]
        self.assertIsNotNone(fit_affine(pairs))

    def test_apply_is_the_identity_when_unfitted(self):
        self.assertEqual(apply_affine(12.3, None), 12.3)

    def test_apply_clamps_at_zero(self):
        self.assertEqual(apply_affine(1.0, (-50.0, 1.0)), 0.0)

    def test_apply_preserves_order_within_a_position(self):
        # This is the whole point of the affine form: it fixes magnitude and
        # cross-position comparability while provably leaving start/sit order
        # alone. fit_affine's slope bounds guarantee b > 0.
        affine = (1.5, 0.8)
        values = [1.0, 4.0, 9.0, 20.0]
        transformed = [apply_affine(v, affine) for v in values]
        self.assertEqual(transformed, sorted(transformed))


class TestIntervalModel(unittest.TestCase):
    def _rows(self, n=800):
        """Residual spread deliberately grows with the projection, which is the
        real, measured behaviour (Breusch-Pagan p from 4e-06 to 2e-110)."""
        out = []
        for i in range(n):
            projected = 1.0 + (i % 20)
            wobble = ((i * 37) % 21 - 10) / 10.0  # -1..1, deterministic
            out.append({
                "position": "WR",
                "projected": projected,
                "actual": projected + wobble * projected * 0.5,
            })
        return out

    def test_unfitted_position_returns_none(self):
        self.assertIsNone(IntervalModel().fit([]).interval("WR", 10.0))

    # -- availability mixture ---------------------------------------------
    # Regression guards for the second audit's finding 1. The band published
    # for a player is the band on his ACTUAL outcome, which is zero whenever he
    # does not play. The previous code multiplied the conditional endpoints by
    # P(play); coverage of that band fell to 26.5% at p=0.50 against a nominal
    # 80%. See docs/research/second-audit-2026-09-02.md section 2.

    def test_probability_of_one_matches_the_conditional_band(self):
        model = IntervalModel().fit(self._rows())
        self.assertEqual(
            model.interval("WR", 10.0),
            model.interval("WR", 10.0, play_probability=1.0),
        )

    def test_floor_is_exactly_zero_below_ninety_percent(self):
        """With a 10th-percentile floor, any miss chance of 10% or more puts the
        10th percentile of the outcome distribution AT zero. Not near zero --
        at it."""
        model = IntervalModel().fit(self._rows())
        for p in (0.90, 0.85, 0.70, 0.50, 0.20):
            low, _ = model.interval("WR", 10.0, play_probability=p)
            self.assertEqual(low, 0.0, f"floor should be exactly 0 at p={p}")

    def test_floor_is_positive_above_ninety_percent(self):
        model = IntervalModel().fit(self._rows())
        low, _ = model.interval("WR", 10.0, play_probability=0.98)
        self.assertGreater(low, 0.0)

    def test_ceiling_is_not_scaled_by_probability(self):
        """The upside is conditional on suiting up, so it decays only slowly as
        P(play) falls. Multiplying by p -- the old behaviour -- would drive it
        toward zero. Guard against a regression to that."""
        model = IntervalModel().fit(self._rows())
        _, full = model.interval("WR", 10.0, play_probability=1.0)
        _, risky = model.interval("WR", 10.0, play_probability=0.70)
        self.assertGreater(risky, 0.70 * full, "ceiling looks scaled by p")
        self.assertLessEqual(risky, full)

    def test_certain_absence_collapses_the_band(self):
        model = IntervalModel().fit(self._rows())
        self.assertEqual(model.interval("WR", 10.0, play_probability=0.0), (0.0, 0.0))

    def test_empirical_coverage_stays_near_nominal_across_probabilities(self):
        """The property that actually matters: an 80% band should contain the
        outcome about 80% of the time, at every play probability."""
        import random

        rows = self._rows(2000)
        model = IntervalModel().fit(rows)
        conditional = [r["actual"] for r in rows if 8.0 <= r["projected"] <= 12.0]
        self.assertGreater(len(conditional), 50)
        rng = random.Random(11)
        for p in (1.0, 0.85, 0.70, 0.50):
            low, high = model.interval("WR", 10.0, play_probability=p)
            hits = 0
            trials = 20000
            for _ in range(trials):
                outcome = rng.choice(conditional) if rng.random() < p else 0.0
                if low <= outcome <= high:
                    hits += 1
            coverage = hits / trials
            self.assertGreater(
                coverage, 0.68, f"coverage {coverage:.3f} too low at p={p}"
            )
            self.assertLess(
                coverage, 0.95, f"coverage {coverage:.3f} too high at p={p}"
            )

    def test_quantile_offset_is_monotone(self):
        model = IntervalModel().fit(self._rows())
        offsets = [model.quantile_offset("WR", 10.0, q) for q in (0.05, 0.25, 0.5, 0.75, 0.95)]
        self.assertEqual(offsets, sorted(offsets))

    def test_interval_brackets_the_projection(self):
        model = IntervalModel().fit(self._rows())
        low, high = model.interval("WR", 10.0)
        self.assertLess(low, 10.0)
        self.assertGreater(high, 10.0)

    def test_interval_widens_with_the_projection(self):
        model = IntervalModel().fit(self._rows())
        small_low, small_high = model.interval("WR", 2.0)
        big_low, big_high = model.interval("WR", 18.0)
        self.assertGreater(big_high - big_low, small_high - small_low)

    def test_floor_is_clamped_at_zero(self):
        model = IntervalModel().fit(self._rows())
        low, _ = model.interval("WR", 0.5)
        self.assertGreaterEqual(low, 0.0)

    def test_extrapolation_is_flat_not_unbounded(self):
        # Widening without bound for an unusually large projection would be a
        # numerical artefact, not a forecast.
        model = IntervalModel().fit(self._rows())
        edge_low, edge_high = model.interval("WR", 1000.0)
        self.assertLess(edge_high - 1000.0, 100.0)

    def test_thin_position_is_not_fitted(self):
        model = IntervalModel().fit(self._rows()[:20])
        self.assertIsNone(model.interval("WR", 10.0))

    def test_rows_with_missing_values_are_skipped(self):
        model = IntervalModel().fit(
            self._rows() + [{"position": "WR", "projected": None, "actual": 5.0}]
        )
        self.assertIsNotNone(model.interval("WR", 10.0))


if __name__ == "__main__":
    unittest.main()
