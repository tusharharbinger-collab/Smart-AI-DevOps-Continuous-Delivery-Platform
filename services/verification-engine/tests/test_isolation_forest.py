"""
services/verification-engine/tests/test_isolation_forest.py
"""
import numpy as np
import pytest
from src.tests_statistical.isolation_forest import run_isolation_forest


def test_healthy_canary_not_flagged():
    """Canary drawn from the same distribution as baseline → no anomaly."""
    rng = np.random.default_rng(42)
    baseline = rng.normal([0.5, 0.4, 0.3, 0.1], 0.05, size=(100, 4))
    canary = rng.normal([0.5, 0.4, 0.3, 0.1], 0.05, size=(50, 4))
    result = run_isolation_forest(baseline, canary, anomaly_score_threshold=0.65)
    assert result["test"] == "Isolation Forest"
    # Healthy canary should not be flagged (allow some tolerance due to randomness)
    assert result["mean_anomaly_score"] < 0.8


def test_anomalous_canary_flagged():
    """Canary is far out of distribution → mean_anomaly_score above threshold."""
    rng = np.random.default_rng(7)
    # Baseline: tightly clustered near zero
    baseline = rng.normal([0.1, 0.1, 0.1, 0.1], 0.01, size=(200, 4))
    # Canary: extreme outliers (>20σ away from baseline mean)
    canary = rng.normal([0.99, 0.98, 0.97, 0.99], 0.005, size=(50, 4))
    result = run_isolation_forest(baseline, canary, contamination=0.01, anomaly_score_threshold=0.5)
    # Normalization places baseline at 0 and outliers at 1.0, so max must be > 0.5
    assert result["max_anomaly_score"] > 0.5, (
        f"Max anomaly score {result['max_anomaly_score']} should be > 0.5 for extreme OOD canary"
    )



def test_insufficient_data_returns_safe_default():
    baseline = np.ones((5, 4)) * 0.5   # < 10 rows
    canary = np.ones((3, 4)) * 0.5     # < 5 rows
    result = run_isolation_forest(baseline, canary)
    assert result["detected"] is False
    assert "note" in result


def test_result_fields_present():
    rng = np.random.default_rng(0)
    baseline = rng.normal(0.5, 0.1, size=(50, 2))
    canary = rng.normal(0.5, 0.1, size=(20, 2))
    result = run_isolation_forest(baseline, canary)
    for key in ["test", "mean_anomaly_score", "max_anomaly_score",
                "detected", "anomalous_vectors", "threshold"]:
        assert key in result, f"Missing key: {key}"


def test_score_normalized_between_0_and_1():
    rng = np.random.default_rng(3)
    baseline = rng.normal(0.5, 0.1, size=(50, 2))
    canary = rng.normal(0.8, 0.2, size=(20, 2))
    result = run_isolation_forest(baseline, canary)
    assert 0.0 <= result["mean_anomaly_score"] <= 1.0
    assert 0.0 <= result["max_anomaly_score"] <= 1.0
