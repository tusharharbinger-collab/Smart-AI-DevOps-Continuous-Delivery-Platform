"""
services/verification-engine/tests/test_business_metric.py

Unit tests for business_metric_test.py — full suite from spec §6.4.
"""
import pytest
from src.tests_statistical.business_metric_test import run_business_metric_test


def test_no_regression_similar_rates():
    """Similar rates, no statistically significant difference."""
    result = run_business_metric_test(
        baseline_success=980, baseline_total=1000,   # 98.0%
        canary_success=975, canary_total=1000,        # 97.5%
    )
    assert result["is_actionable_regression"] is False
    assert result["baseline_rate"] == pytest.approx(0.980, abs=1e-4)
    assert result["canary_rate"] == pytest.approx(0.975, abs=1e-4)


def test_clear_regression_uses_chi_square():
    """Large samples + sharp drop → chi-square is chosen, regression detected."""
    result = run_business_metric_test(
        baseline_success=980, baseline_total=1000,   # 98.0%
        canary_success=850, canary_total=1000,        # 85.0% — sharp drop
    )
    assert result["used_fisher_exact"] is False     # large samples → chi-square
    assert result["is_significant"] is True
    assert result["is_actionable_regression"] is True
    assert result["p_value"] < 0.001


def test_small_sample_uses_fishers_exact():
    """Small counts → expected cell < 5 → Fisher's exact is chosen."""
    result = run_business_metric_test(
        baseline_success=9, baseline_total=10,
        canary_success=3, canary_total=10,
    )
    assert result["used_fisher_exact"] is True


def test_empty_baseline_returns_safe_default():
    """Zero baseline total → no crash, returns safe dict."""
    result = run_business_metric_test(
        baseline_success=0, baseline_total=0,
        canary_success=50, canary_total=100,
    )
    assert result["is_significant"] is False
    assert result["is_actionable_regression"] is False
    assert "note" in result


def test_increase_direction_increase_is_bad():
    """When direction='increase_is_bad', a higher canary rate is a regression."""
    result = run_business_metric_test(
        baseline_success=50, baseline_total=1000,   # 5.0% error rate
        canary_success=100, canary_total=1000,       # 10.0% error rate
        direction="increase_is_bad",
    )
    assert result["is_actionable_regression"] is True


def test_no_regression_when_rates_identical():
    """Identical rates → p-value = 1.0 → no regression."""
    result = run_business_metric_test(
        baseline_success=500, baseline_total=1000,
        canary_success=500, canary_total=1000,
    )
    assert result["is_actionable_regression"] is False
    assert result["p_value"] == pytest.approx(1.0, abs=0.1)


def test_delta_percent_computed_correctly():
    """delta_percent = (canary - baseline) / baseline * 100."""
    result = run_business_metric_test(
        baseline_success=800, baseline_total=1000,   # 80%
        canary_success=720, canary_total=1000,        # 72%
    )
    expected_delta = (0.72 - 0.80) / 0.80 * 100
    assert result["delta_percent"] == pytest.approx(expected_delta, abs=0.1)
