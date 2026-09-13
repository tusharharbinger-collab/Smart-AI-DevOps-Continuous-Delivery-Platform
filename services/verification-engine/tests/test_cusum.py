"""
services/verification-engine/tests/test_cusum.py
"""
import numpy as np
import pytest
from src.tests_statistical.cusum import run_cusum


def test_no_changepoint_stable_series():
    rng = np.random.default_rng(42)
    series = rng.normal(0.5, 0.05, 200)
    result = run_cusum(series, baseline_mean=0.5, baseline_std=0.05)
    assert result["detected"] is False


def test_detects_step_change():
    """Clear step change at midpoint — CUSUM must detect it."""
    rng = np.random.default_rng(7)
    baseline_phase = rng.normal(0.5, 0.05, 100)
    shifted_phase = rng.normal(0.8, 0.05, 100)   # 6σ shift
    series = np.concatenate([baseline_phase, shifted_phase])
    result = run_cusum(series, baseline_mean=0.5, baseline_std=0.05, k=0.5, h=5.0)
    assert result["detected"] is True
    assert result["breach_positive"] is True


def test_detects_slow_drift():
    """Gradual 0.1-sigma-per-step drift over 200 samples."""
    rng = np.random.default_rng(99)
    baseline_mean, baseline_std = 0.042, 0.005
    drift = np.array([baseline_mean + 0.1 * baseline_std * i / 10 for i in range(200)])
    noisy = drift + rng.normal(0, baseline_std * 0.3, 200)
    result = run_cusum(noisy, baseline_mean=baseline_mean, baseline_std=baseline_std, k=0.5, h=5.0)
    assert result["detected"] is True, "CUSUM must catch slow latency creep"


def test_zero_variance_returns_no_detection():
    series = np.full(100, 0.5)
    result = run_cusum(series, baseline_mean=0.5, baseline_std=0.0)
    assert result["detected"] is False
    assert "note" in result


def test_breach_index_is_valid_when_detected():
    rng = np.random.default_rng(3)
    series = np.concatenate([rng.normal(0, 1, 50), rng.normal(5, 1, 50)])
    result = run_cusum(series, baseline_mean=0.0, baseline_std=1.0, h=3.0)
    if result["detected"]:
        assert result["breach_index"] is not None
        assert 0 <= result["breach_index"] < 100
