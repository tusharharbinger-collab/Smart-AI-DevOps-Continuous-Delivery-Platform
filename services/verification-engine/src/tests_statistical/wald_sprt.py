"""
services/verification-engine/src/tests_statistical/wald_sprt.py

Wald's Sequential Probability Ratio Test — streaming, online, no fixed sample size.
Reference: Wald, A. (1947). Sequential Analysis. Wiley.

H₀: canary error probability p = p₀  (acceptable baseline rate, e.g. 0.5%)
H₁: canary error probability p = p₁  (unacceptable regression rate, e.g. 2.0%)

Decision boundaries (Wald's original formulation):
  A = ln((1-β)/α)  — Λ ≥ A  ⟹  REJECT H0 (trigger rollback)
  B = ln(β/(1-α))  — Λ ≤ B  ⟹  ACCEPT H0 (safe to promote)
"""
import numpy as np
from dataclasses import dataclass, field


@dataclass
class SPRTState:
    log_likelihood_ratio: float = 0.0
    total_requests: int = 0
    total_errors: int = 0
    decision: str = "CONTINUE"   # "CONTINUE" | "REJECT_H0" | "ACCEPT_H0"
    history: list = field(default_factory=list)   # LLR values for visualisation


def sprt_update(
    state: SPRTState,
    is_error: bool,
    p0: float = 0.005,
    p1: float = 0.020,
    alpha: float = 0.01,
    beta: float = 0.10,
) -> SPRTState:
    """
    Process one observation and update the sequential LLR.

    Under H₁ (error):  contribution = log(p1/p0)
    Under H₀ (success): contribution = log((1-p1)/(1-p0))
    """
    A = np.log((1 - beta) / alpha)   # upper boundary  ≈ +2.30 for α=0.01, β=0.10
    B = np.log(beta / (1 - alpha))   # lower boundary  ≈ −2.30

    x_i = 1 if is_error else 0
    delta = (
        np.log(p1 / p0) if x_i == 1
        else np.log((1 - p1) / (1 - p0))
    )
    new_llr = state.log_likelihood_ratio + delta

    if new_llr >= A:
        decision = "REJECT_H0"      # rollback
    elif new_llr <= B:
        decision = "ACCEPT_H0"      # promote
    else:
        decision = "CONTINUE"

    new_history = state.history + [round(new_llr, 4)]
    return SPRTState(
        log_likelihood_ratio=new_llr,
        total_requests=state.total_requests + 1,
        total_errors=state.total_errors + x_i,
        decision=decision,
        history=new_history,
    )


def sprt_thresholds(alpha: float = 0.01, beta: float = 0.10) -> tuple[float, float]:
    """Returns (A, B) — upper and lower Wald boundaries."""
    return float(np.log((1 - beta) / alpha)), float(np.log(beta / (1 - alpha)))


def run_sprt_batch(
    errors: list[bool],
    p0: float = 0.005,
    p1: float = 0.020,
    alpha: float = 0.01,
    beta: float = 0.10,
) -> SPRTState:
    """
    Convenience function: replay a list of observations through the SPRT.
    Returns the final state after all observations.
    """
    state = SPRTState()
    for is_error in errors:
        state = sprt_update(state, is_error, p0=p0, p1=p1, alpha=alpha, beta=beta)
        if state.decision in ("REJECT_H0", "ACCEPT_H0"):
            break   # stop as soon as a decision is reached
    return state
