"""Unit-tests for predictions-modulet (rene funktioner — ingen DB)."""
from __future__ import annotations

import pytest

from predictions import classify_risk, linear_regression, stddev


class TestLinearRegression:
    def test_perfect_positive_line(self):
        slope, intercept = linear_regression(
            [0.0, 1.0, 2.0, 3.0], [10.0, 12.0, 14.0, 16.0]
        )
        assert slope == pytest.approx(2.0)
        assert intercept == pytest.approx(10.0)

    def test_perfect_negative_line(self):
        slope, intercept = linear_regression([0.0, 1.0, 2.0], [100.0, 90.0, 80.0])
        assert slope == pytest.approx(-10.0)
        assert intercept == pytest.approx(100.0)

    def test_flat_line_has_zero_slope(self):
        slope, intercept = linear_regression([0.0, 1.0, 2.0], [50.0, 50.0, 50.0])
        assert slope == 0.0
        assert intercept == pytest.approx(50.0)

    def test_single_point_returns_zero_slope(self):
        slope, intercept = linear_regression([5.0], [42.0])
        assert slope == 0.0
        assert intercept == 42.0

    def test_empty_input_returns_safe_defaults(self):
        slope, intercept = linear_regression([], [])
        assert slope == 0.0
        assert intercept == 0.0

    def test_identical_x_returns_zero_slope(self):
        # Vertical "line" — bør ikke divide-by-zero
        slope, intercept = linear_regression([1.0, 1.0, 1.0], [10.0, 20.0, 30.0])
        assert slope == 0.0
        assert intercept == pytest.approx(20.0)


class TestStddev:
    def test_known_dataset(self):
        # Sample-stddev af [2,4,4,4,5,5,7,9] = sqrt(32/7) ≈ 2.138
        result = stddev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert result == pytest.approx(2.138, abs=0.01)

    def test_constant_input_is_zero(self):
        assert stddev([5.0, 5.0, 5.0]) == 0.0

    def test_single_value_is_zero(self):
        assert stddev([42.0]) == 0.0

    def test_empty_input_is_zero(self):
        assert stddev([]) == 0.0


class TestClassifyRisk:
    def test_above_threshold_is_always_high(self):
        # Aktuel temp over 70°C → HIGH uanset andre signaler
        assert classify_risk(75.0, 60.0, 2.0, 0.0) == "HIGH"
        assert classify_risk(70.5, 60.0, 2.0, -5.0) == "HIGH"

    def test_strong_trend_with_outlier_is_high(self):
        # slope > 1°C/dag og z_score > 1.5
        assert classify_risk(65.0, 60.0, 2.0, 1.5) == "HIGH"

    def test_moderate_trend_alone_is_medium(self):
        # slope > 0.5 men hverken stærk eller outlier
        assert classify_risk(60.0, 58.0, 2.0, 0.7) == "MEDIUM"

    def test_outlier_alone_is_medium(self):
        # z_score > 1.5 men flad trend
        assert classify_risk(65.0, 60.0, 2.0, 0.0) == "MEDIUM"

    def test_stable_device_is_low(self):
        assert classify_risk(55.0, 55.0, 2.0, 0.1) == "LOW"

    def test_zero_stddev_does_not_crash(self):
        # Konstant baseline — z_score-beregning skal ikke divide-by-zero
        assert classify_risk(50.0, 50.0, 0.0, 0.0) == "LOW"
