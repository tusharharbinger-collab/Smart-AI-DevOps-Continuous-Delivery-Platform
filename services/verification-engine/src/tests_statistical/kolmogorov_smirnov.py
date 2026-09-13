"""
services/verification-engine/src/tests_statistical/kolmogorov_smirnov.py

Two-sample Kolmogorov-Smirnov test — used as a secondary latency check
alongside Mann-Whitney to detect distributional shifts (not just rank shifts).
Reference: Kolmogorov (1933), Smirnov (1948).
"""
import numpy as np
from scipy import stats


def run_kolmogorov_smirnov(
    baseline_samples: np.ndarray,
    canary_samples: np.ndarray,
    alpha: float = 0.05,
) -> dict:
    """
    Two-sample KS test: H₀ = both samples come from the same distribution.

    The KS statistic D is the maximum absolute difference between the two ECDFs.
    This complements Mann-Whitney: MW tests stochastic dominance, KS tests
    any distributional difference (shape, tail weight, multimodality).
    """
    if len(baseline_samples) == 0 or len(canary_samples) == 0:
        raise ValueError("KS test requires non-empty samples")

    stat, p_value = stats.ks_2samp(baseline_samples, canary_samples)

    return {
        "test": "Kolmogorov-Smirnov",
        "ks_statistic": float(stat),
        "p_value": float(p_value),
        "is_significant": bool(p_value < alpha),
        "n_baseline": len(baseline_samples),
        "n_canary": len(canary_samples),
    }
