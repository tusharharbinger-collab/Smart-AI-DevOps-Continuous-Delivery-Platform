"""
services/verification-engine/src/tests_statistical/mann_whitney.py

Two-Sample Mann-Whitney U test (Wilcoxon rank-sum) for latency comparison.
Reference: Mann, H.B. & Whitney, D.R. (1947). Ann. Math. Stat. 18(1):50–60.

H₀: P(canary_latency > baseline_latency) = 0.50  (stochastic equality)
H₁: P(canary_latency > baseline_latency) > 0.50  (canary is slower)

Uses scipy's mannwhitneyu with the 'greater' alternative (one-sided),
which tests whether canary observations tend to be LARGER than baseline.
Reports the Common Language Effect Size (CLES / Vargha-Delaney A statistic).
"""
import numpy as np
from scipy import stats


def run_mann_whitney(
    baseline_samples: np.ndarray,
    canary_samples: np.ndarray,
    alpha: float = 0.05,
    direction: str = "increase_is_bad",   # latency increase = regression
) -> dict:
    """
    Run Mann-Whitney U test between baseline and canary latency samples.

    Returns a dict with:
      test            — test name (citation-builder compatible)
      p_value         — one-sided p-value
      effect_size_cles — Common Language Effect Size ∈ [0, 1]
      z_statistic     — standardised statistic
      is_significant  — p_value < alpha
      is_actionable_regression — CLES ≥ 0.65 AND significant (both must be true)
      baseline_median, canary_median — for citation builder
    """
    n1 = len(baseline_samples)
    n2 = len(canary_samples)

    if n1 == 0 or n2 == 0:
        raise ValueError(
            f"Mann-Whitney requires non-empty samples; got n_baseline={n1}, n_canary={n2}"
        )

    if direction == "increase_is_bad":
        # test whether canary > baseline
        stat, p_value = stats.mannwhitneyu(
            canary_samples, baseline_samples, alternative="greater"
        )
    else:
        stat, p_value = stats.mannwhitneyu(
            baseline_samples, canary_samples, alternative="greater"
        )

    # CLES (A) = U / (n1 * n2) — probability that a random canary obs > baseline obs
    A = stat / (n1 * n2)

    # Z-statistic (continuity correction)
    mu_u = (n1 * n2) / 2
    sigma_u = np.sqrt((n1 * n2 * (n1 + n2 + 1)) / 12)
    z = (stat - mu_u + 0.5) / sigma_u

    return {
        "test": "Mann-Whitney U",
        "p_value": float(p_value),
        "effect_size_cles": float(A),
        "z_statistic": float(z),
        "n_baseline": n1,
        "n_canary": n2,
        "is_significant": bool(p_value < alpha),
        # Both effect AND significance required — prevents false positives from large N
        "is_actionable_regression": bool(A >= 0.65 and p_value < alpha),
        "baseline_median": float(np.median(baseline_samples)),
        "canary_median": float(np.median(canary_samples)),
        "baseline_mean": float(np.mean(baseline_samples)),
        "canary_mean": float(np.mean(canary_samples)),
    }
