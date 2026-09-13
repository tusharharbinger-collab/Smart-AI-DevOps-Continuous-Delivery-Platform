"""
services/verification-engine/src/tests_statistical/welch_t.py

Welch's two-sample t-test (unequal variances) — supplementary to Mann-Whitney
for latency. Used when both sample sizes are large (CLT applies) and the
researcher wants a parametric complement to the non-parametric MW test.
Reference: Welch, B.L. (1947). Biometrika 34(1-2):28–35.
"""
import numpy as np
from scipy import stats


def run_welch_t(
    baseline_samples: np.ndarray,
    canary_samples: np.ndarray,
    alpha: float = 0.05,
) -> dict:
    """
    Welch's t-test: H₀ = equal means. Does NOT assume equal variance.
    """
    if len(baseline_samples) == 0 or len(canary_samples) == 0:
        raise ValueError("Welch t-test requires non-empty samples")

    stat, p_value = stats.ttest_ind(
        canary_samples, baseline_samples, equal_var=False, alternative="greater"
    )

    return {
        "test": "Welch t-test",
        "t_statistic": float(stat),
        "p_value": float(p_value),
        "is_significant": bool(p_value < alpha),
        "canary_mean": float(np.mean(canary_samples)),
        "baseline_mean": float(np.mean(baseline_samples)),
        "n_baseline": len(baseline_samples),
        "n_canary": len(canary_samples),
    }
