"""
services/verification-engine/src/tests_statistical/cusum.py

Page-Hinkley CUSUM (Cumulative Sum) for online change-point detection.
Reference: Page, E.S. (1954). Biometrika 41(1-2):100-115.

Custom NumPy implementation — NOT the ruptures library (ruptures is
offline/batch; this runs online, one observation at a time, as required
for streaming CPU/memory saturation signals).

Parameters
----------
k : float
    Allowance (slack) parameter — observations must exceed μ + k·σ before
    CUSUM accumulates. Typical value: 0.5.
h : float
    Decision threshold (control limit). Alarm when S⁺ or S⁻ ≥ h.
    Typical value: 5.0 for 5σ alert level.
"""
import numpy as np


def run_cusum(
    canary_series: np.ndarray,
    baseline_mean: float,
    baseline_std: float,
    k: float = 0.5,
    h: float = 5.0,
) -> dict:
    """
    Two-sided CUSUM on z-scored canary observations.

    Returns
    -------
    dict with keys:
      test            — "CUSUM"
      detected        — True if a change-point was found
      breach_positive — upward shift detected (latency/error increase)
      breach_negative — downward shift detected
      breach_index    — sample index of first breach, or None
      final_S_pos     — final positive accumulator
      final_S_neg     — final negative accumulator
    """
    if baseline_std < 1e-9:
        return {
            "test": "CUSUM", "detected": False,
            "note": "zero baseline variance — CUSUM not applicable",
            "breach_positive": False, "breach_negative": False,
            "breach_index": None, "final_S_pos": 0.0, "final_S_neg": 0.0,
        }

    z = (canary_series - baseline_mean) / baseline_std

    S_pos = np.zeros(len(z) + 1)
    S_neg = np.zeros(len(z) + 1)
    breach_pos = breach_neg = False
    breach_index: int | None = None

    for i, z_t in enumerate(z):
        S_pos[i + 1] = max(0.0, S_pos[i] + z_t - k)
        S_neg[i + 1] = max(0.0, S_neg[i] - z_t - k)

        if S_pos[i + 1] >= h and not breach_pos:
            breach_pos = True
            breach_index = i

        if S_neg[i + 1] >= h and not breach_neg:
            breach_neg = True
            if breach_index is None:
                breach_index = i
            else:
                breach_index = min(breach_index, i)

    return {
        "test": "CUSUM",
        "detected": breach_pos or breach_neg,
        "breach_positive": breach_pos,
        "breach_negative": breach_neg,
        "breach_index": breach_index,
        "final_S_pos": float(S_pos[-1]),
        "final_S_neg": float(S_neg[-1]),
    }
