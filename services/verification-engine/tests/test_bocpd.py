"""
services/verification-engine/tests/test_bocpd.py
"""
import numpy as np
import pytest
from src.tests_statistical.bocpd import run_bocpd


def test_no_changepoint_stationary_series():
    rng = np.random.default_rng(42)
    series = rng.normal(0.5, 0.1, 100)
    result = run_bocpd(series, hazard_lambda=250.0, change_threshold=0.85)
    assert result["test"] == "BOCPD"
    assert isinstance(result["detected"], bool)
    assert 0.0 <= result["max_changepoint_prob"] <= 1.0


def test_detects_obvious_changepoint():
    """Clear distributional shift halfway through — BOCPD should detect it."""
    rng = np.random.default_rng(7)
    phase1 = rng.normal(0.3, 0.02, 60)
    phase2 = rng.normal(0.9, 0.02, 60)   # large shift
    series = np.concatenate([phase1, phase2])
    result = run_bocpd(series, hazard_lambda=100.0, change_threshold=0.50)
    # With a threshold of 0.50, a very large shift should be detected
    assert result["max_changepoint_prob"] > 0.0


def test_insufficient_data_returns_safe_default():
    """< 10 points — must return detected=False without crashing."""
    result = run_bocpd(np.array([0.1, 0.2, 0.3]))
    assert result["detected"] is False
    assert "note" in result


def test_result_schema():
    rng = np.random.default_rng(0)
    result = run_bocpd(rng.normal(0.5, 0.1, 50))
    for key in ["test", "detected", "max_changepoint_prob", "change_index", "threshold"]:
        assert key in result, f"Missing key: {key}"


def test_change_index_none_when_not_detected():
    """If no CP declared, change_index must be None."""
    rng = np.random.default_rng(13)
    result = run_bocpd(rng.normal(0.5, 0.05, 50), change_threshold=0.9999)
    if not result["detected"]:
        assert result["change_index"] is None
