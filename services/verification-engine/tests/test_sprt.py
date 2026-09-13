"""
services/verification-engine/tests/test_sprt.py

Unit tests for Wald SPRT — known-input / known-output verification.
"""
import numpy as np
import pytest
from src.tests_statistical.wald_sprt import (
    SPRTState, sprt_update, sprt_thresholds, run_sprt_batch
)


def test_sprt_initial_state_is_continue():
    state = SPRTState()
    assert state.decision == "CONTINUE"
    assert state.log_likelihood_ratio == pytest.approx(0.0)
    assert state.total_requests == 0
    assert state.total_errors == 0


def test_sprt_rejects_on_high_error_rate():
    """Stream of errors well above p1 → should REJECT_H0."""
    state = SPRTState()
    # Inject 30 errors in 40 requests (75% error rate >> p1=2%)
    for i in range(40):
        state = sprt_update(state, is_error=(i < 30), p0=0.005, p1=0.020)
        if state.decision == "REJECT_H0":
            break
    assert state.decision == "REJECT_H0", "SPRT failed to reject H0 on high error rate"


def test_sprt_accepts_on_clean_traffic():
    """Long stream of clean requests → should ACCEPT_H0."""
    observations = [False] * 500   # 0 errors
    state = run_sprt_batch(observations, p0=0.005, p1=0.020)
    assert state.decision == "ACCEPT_H0", "SPRT failed to accept H0 on clean traffic"


def test_sprt_continues_on_ambiguous_traffic():
    """Error rate near p0 (1 error in 200 requests = 0.5%) → ACCEPT_H0 or CONTINUE, not REJECT."""
    # Uniformly intersperse 1 error per 200 requests — rate exactly at p0
    # This is NOT a concentrated burst (which would REJECT), but steady ~p0 rate
    rng = __import__('numpy').random.default_rng(42)
    observations = [False] * 200
    # Place the single error at position 100 (halfway through, least likely to REJECT)
    observations[100] = True
    state = run_sprt_batch(observations, p0=0.005, p1=0.020)
    # Near-p0 error rate must not be classified as a regression
    assert state.decision in ("CONTINUE", "ACCEPT_H0"), (
        f"Got {state.decision} with LLR={state.log_likelihood_ratio:.3f} — "
        "error rate at p0 should not reject H0"
    )


def test_sprt_thresholds_positive_negative():
    A, B = sprt_thresholds(alpha=0.01, beta=0.10)
    assert A > 0, "Upper bound A must be positive"
    assert B < 0, "Lower bound B must be negative"


def test_sprt_history_grows_with_updates():
    state = SPRTState()
    for _ in range(10):
        state = sprt_update(state, is_error=False)
    assert len(state.history) == 10


def test_sprt_total_requests_and_errors_tracked():
    state = SPRTState()
    for i in range(5):
        state = sprt_update(state, is_error=(i % 2 == 0))
    assert state.total_requests == 5
    assert state.total_errors == 3  # i=0,2,4 are errors
