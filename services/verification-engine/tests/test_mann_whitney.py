"""
services/verification-engine/tests/test_mann_whitney.py
"""
import numpy as np
import pytest
from src.tests_statistical.mann_whitney import run_mann_whitney


def test_no_regression_identical_distributions():
    rng = np.random.default_rng(42)
    baseline = rng.normal(0.042, 0.005, 500)
    canary = rng.normal(0.042, 0.005, 500)
    result = run_mann_whitney(baseline, canary)
    assert result["is_actionable_regression"] is False


def test_clear_regression_detected():
    rng = np.random.default_rng(42)
    baseline = rng.normal(0.042, 0.005, 500)
    canary = rng.normal(0.100, 0.010, 500)   # ~2.4× slower
    result = run_mann_whitney(baseline, canary)
    assert result["is_significant"] is True
    assert result["effect_size_cles"] > 0.65
    assert result["is_actionable_regression"] is True


def test_empty_samples_raises_value_error():
    with pytest.raises(ValueError):
        run_mann_whitney(np.array([]), np.array([]))


def test_cles_near_half_for_equal_distributions():
    rng = np.random.default_rng(0)
    b = rng.normal(0, 1, 2000)
    c = rng.normal(0, 1, 2000)
    result = run_mann_whitney(b, c)
    assert abs(result["effect_size_cles"] - 0.50) < 0.05


def test_result_fields_present():
    rng = np.random.default_rng(1)
    b = rng.normal(0.04, 0.005, 200)
    c = rng.normal(0.04, 0.005, 200)
    result = run_mann_whitney(b, c)
    for key in ["test", "p_value", "effect_size_cles", "z_statistic",
                "n_baseline", "n_canary", "is_significant",
                "is_actionable_regression", "baseline_median", "canary_median"]:
        assert key in result, f"Missing key: {key}"
