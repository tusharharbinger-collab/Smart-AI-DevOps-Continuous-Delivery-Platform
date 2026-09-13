"""
tests/adversarial/test_statistical_robustness.py

Spec §10a / §14.1: noisy metrics, missing telemetry, slow latency creep.
Every test here asserts the SYSTEM DEGRADES GRACEFULLY — never crashes,
never produces a confident wrong answer.
"""
import numpy as np
import pytest

from preprocessing.iqr_filter import apply_iqr_filter
from scoring.confidence import compute_confidence
from tests_statistical.cusum import run_cusum
from tests_statistical.mann_whitney import run_mann_whitney


def test_noisy_spike_does_not_cause_false_rollback():
    """10 extreme 10,000ms spikes injected into an otherwise healthy 42ms canary."""
    baseline = np.random.normal(0.042, 0.005, 500)
    canary_clean = np.random.normal(0.042, 0.005, 490)
    canary_with_spikes = np.concatenate([canary_clean, np.full(10, 10.0)])

    filtered_canary = apply_iqr_filter(canary_with_spikes)
    result = run_mann_whitney(baseline, filtered_canary)

    assert result["is_actionable_regression"] is False, (
        "IQR filtering failed to prevent transient spikes from triggering a false regression"
    )


def test_missing_telemetry_degrades_confidence_not_crash():
    """Only 5 samples collected (Prometheus scrape mostly failing)."""
    confidence = compute_confidence(
        n_baseline=500, n_canary=5,
        var_baseline=0.001, var_canary=0.001,
        elapsed_seconds=120, min_eval_seconds=120,
        n_required=100,
    )
    assert 0.0 <= confidence < 0.30, "Low sample count must yield low confidence, not a crash or a high score"


def test_empty_canary_samples_raises_not_silently_wrong():
    """
    Mann-Whitney on empty samples must fail loudly (ValueError) rather than
    silently returning a bogus result — engine.py's dispatcher wraps this call
    and converts the exception into a worst-case DEGRADED score, never an
    unhandled 500 or (worse) a falsely confident HEALTHY.
    """
    with pytest.raises(ValueError):
        run_mann_whitney(np.array([]), np.array([]))


def test_slow_latency_creep_detected_by_cusum_not_missed():
    """Gradual 0.1-sigma-per-step drift over 200 samples — a single-window
    Mann-Whitney snapshot could miss this; CUSUM must catch the accumulation."""
    baseline_mean, baseline_std = 0.042, 0.005
    drift = np.array([baseline_mean + 0.1 * baseline_std * i / 10 for i in range(200)])
    noisy_drift = drift + np.random.normal(0, baseline_std * 0.3, 200)

    result = run_cusum(noisy_drift, baseline_mean, baseline_std, k=0.5, h=5.0)
    assert result["detected"] is True, "CUSUM failed to detect slow, gradual latency creep"


def test_symmetric_traffic_surge_does_not_false_positive():
    """400% traffic surge hits BOTH cohorts equally — must NOT trigger rollback."""
    surge_baseline = np.random.normal(0.084, 0.010, 2000)  # both cohorts degrade equally
    surge_canary = np.random.normal(0.084, 0.010, 2000)
    result = run_mann_whitney(surge_baseline, surge_canary)
    assert abs(result["effect_size_cles"] - 0.50) < 0.05, "Symmetric surge falsely flagged as canary regression"


def test_zero_variance_baseline_does_not_crash_cusum():
    """A perfectly flat baseline (zero variance) must not divide-by-zero."""
    canary = np.random.normal(0.05, 0.01, 100)
    result = run_cusum(canary, baseline_mean=0.05, baseline_std=0.0)
    assert result["detected"] is False
    assert "note" in result
