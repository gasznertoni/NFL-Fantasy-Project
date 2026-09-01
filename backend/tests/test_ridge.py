"""Unit tests for backend/ridge.py -- the shared linear-algebra kit.

These are the arithmetic guarantees every model in the backend rests on, so
they are tested against closed-form answers rather than against each other.
"""

import math
import unittest

from ridge import (
    DEFAULT_Z_CLIP,
    column_stats,
    logistic,
    median,
    num,
    predict_linear,
    predict_probability,
    ridge,
    solve,
    standardize,
)


class TestNum(unittest.TestCase):
    """`num` is the gate everything else relies on: pandas hands back NaN, not
    None, for a missing numeric, and NaN is truthy."""

    def test_nan_and_inf_are_missing(self):
        self.assertIsNone(num(float("nan")))
        self.assertIsNone(num(float("inf")))
        self.assertIsNone(num(float("-inf")))

    def test_none_and_junk_are_missing(self):
        self.assertIsNone(num(None))
        self.assertIsNone(num("abc"))
        self.assertIsNone(num([1]))

    def test_zero_is_data_not_missing(self):
        self.assertEqual(num(0), 0.0)
        self.assertEqual(num(0.0), 0.0)

    def test_numeric_strings_and_bools_coerce(self):
        self.assertEqual(num("2.5"), 2.5)
        self.assertEqual(num(True), 1.0)
        self.assertEqual(num(False), 0.0)


class TestMedian(unittest.TestCase):
    def test_empty_is_none_not_zero(self):
        self.assertIsNone(median([]))

    def test_odd_and_even_lengths(self):
        self.assertEqual(median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(median([4.0, 1.0, 3.0, 2.0]), 2.5)


class TestColumnStats(unittest.TestCase):
    def test_mean_and_sample_sd(self):
        mean, std = column_stats([[1.0], [2.0], [3.0]])
        self.assertAlmostEqual(mean[0], 2.0)
        self.assertAlmostEqual(std[0], 1.0)  # sample sd, n-1

    def test_constant_column_gets_unit_sd(self):
        # Otherwise standardising divides by zero. Unit sd sends the column to
        # 0 after centring, which the penalty then correctly ignores.
        mean, std = column_stats([[5.0], [5.0], [5.0]])
        self.assertEqual(std[0], 1.0)
        self.assertEqual(standardize([5.0], mean, std)[0], 0.0)


class TestStandardize(unittest.TestCase):
    def test_clip_bounds_extremes_both_ways(self):
        self.assertEqual(standardize([1e9], [0.0], [1.0], clip=DEFAULT_Z_CLIP)[0], DEFAULT_Z_CLIP)
        self.assertEqual(standardize([-1e9], [0.0], [1.0], clip=DEFAULT_Z_CLIP)[0], -DEFAULT_Z_CLIP)

    def test_unclipped_by_default(self):
        self.assertEqual(standardize([1e9], [0.0], [1.0])[0], 1e9)

    def test_values_inside_the_bound_are_untouched(self):
        self.assertAlmostEqual(standardize([2.0], [0.0], [1.0], clip=4.0)[0], 2.0)


class TestSolve(unittest.TestCase):
    def test_solves_a_known_system(self):
        # 2x + y = 5 ; x + 3y = 10  ->  x = 1, y = 3
        self.assertEqual([round(v, 9) for v in solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])], [1.0, 3.0])

    def test_singular_system_returns_finite_values(self):
        out = solve([[1.0, 1.0], [1.0, 1.0]], [2.0, 2.0])
        self.assertTrue(all(math.isfinite(v) for v in out))

    def test_partial_pivoting_handles_a_zero_leading_pivot(self):
        # Without pivoting this divides by zero on the first column.
        self.assertEqual([round(v, 9) for v in solve([[0.0, 1.0], [1.0, 0.0]], [3.0, 2.0])], [2.0, 3.0])


class TestRidge(unittest.TestCase):
    def test_recovers_a_known_linear_relationship(self):
        design = [[x1, x2] for x1 in range(-3, 4) for x2 in range(-3, 4)]
        targets = [2 + 3 * a - b for a, b in design]
        beta = ridge(design, targets, alpha=0.001)
        self.assertAlmostEqual(beta[0], 2.0, places=2)
        self.assertAlmostEqual(beta[1], 3.0, places=2)
        self.assertAlmostEqual(beta[2], -1.0, places=2)

    def test_intercept_is_not_penalised(self):
        # A penalised intercept would be dragged below the true level here.
        beta = ridge([[-1.0], [0.0], [1.0]], [10.0, 10.0, 10.0], alpha=500.0)
        self.assertAlmostEqual(beta[0], 10.0, places=6)

    def test_larger_alpha_shrinks_slopes_toward_zero(self):
        design = [[float(x)] for x in range(-5, 6)]
        targets = [3.0 * x for x in range(-5, 6)]
        weak = ridge(design, targets, alpha=0.01)
        strong = ridge(design, targets, alpha=100.0)
        self.assertLess(abs(strong[1]), abs(weak[1]))

    def test_collinear_features_stay_finite(self):
        design = [[x, 2 * x] for x in range(-5, 6)]
        targets = [3.0 * x for x in range(-5, 6)]
        self.assertTrue(all(math.isfinite(b) for b in ridge(design, targets, alpha=1.0)))


class TestLogistic(unittest.TestCase):
    def test_recovers_a_separating_direction(self):
        design = [[float(x)] for x in range(-10, 11)]
        targets = [1.0 if x > 0 else 0.0 for x in range(-10, 11)]
        beta = logistic(design, targets, l2=0.01)
        self.assertGreater(beta[1], 0.0)
        self.assertGreater(predict_probability(beta, [8.0]), 0.9)
        self.assertLess(predict_probability(beta, [-8.0]), 0.1)

    def test_perfect_separation_does_not_blow_up(self):
        # The real availability data is nearly separable -- "Out" means
        # P(play) = 0.0006 -- so the working weight p*(1-p) hits zero and the
        # Hessian goes singular unless it is floored.
        design = [[0.0]] * 30 + [[1.0]] * 30
        targets = [0.0] * 30 + [1.0] * 30
        beta = logistic(design, targets, l2=1.0)
        self.assertTrue(all(math.isfinite(b) for b in beta))
        self.assertGreater(predict_probability(beta, [1.0]), predict_probability(beta, [0.0]))

    def test_probabilities_stay_in_the_unit_interval(self):
        beta = logistic([[float(x)] for x in range(-5, 6)],
                        [1.0 if x > 0 else 0.0 for x in range(-5, 6)])
        for value in (-1e6, 0.0, 1e6):
            p = predict_probability(beta, [value])
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_intercept_alone_recovers_the_base_rate(self):
        targets = [1.0] * 70 + [0.0] * 30
        beta = logistic([[] for _ in targets], targets, l2=1e-9)
        self.assertAlmostEqual(predict_probability(beta, []), 0.7, places=2)


class TestPredictHelpers(unittest.TestCase):
    def test_predict_linear_is_intercept_plus_dot_product(self):
        self.assertAlmostEqual(predict_linear([1.0, 2.0, 3.0], [10.0, 100.0]), 1 + 20 + 300)

    def test_predict_probability_is_the_logistic_of_predict_linear(self):
        beta, x = [0.5, 1.5], [2.0]
        self.assertAlmostEqual(
            predict_probability(beta, x), 1 / (1 + math.exp(-predict_linear(beta, x)))
        )


if __name__ == "__main__":
    unittest.main()
