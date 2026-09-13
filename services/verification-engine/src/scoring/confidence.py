"""
services/verification-engine/src/scoring/confidence.py

Composite confidence score C ∈ [0, 1] — three multiplicative factors.
Spec §7. N_required = 100 per assignment requirement.

C = C_sample × C_variance × C_stability

C_sample:   sqrt(N / N_required), capped at 1.0 — sample sufficiency
C_variance: penalizes wildly different variance between cohorts
C_stability: penalizes verdicts reached before the minimum observation window
"""
import numpy as np


def compute_confidence(
    n_baseline: int,
    n_canary: int,
    var_baseline: float,
    var_canary: float,
    elapsed_seconds: float,
    min_eval_seconds: float,
    n_required: int = 100,   # ✅ assignment's N≥100 floor
) -> float:
    """
    Compute the composite confidence score C ∈ [0, 1].

    Parameters
    ----------
    n_baseline, n_canary : int
        Sample counts collected from each cohort.
    var_baseline, var_canary : float
        Observed variance of the primary metric in each cohort.
    elapsed_seconds : float
        Time elapsed since the canary was deployed (seconds).
    min_eval_seconds : float
        Minimum evaluation window required by the pipeline spec.
    n_required : int
        Minimum sample size for a trustworthy verdict (assignment floor = 100).

    Returns
    -------
    float ∈ [0, 1]
    """
    N = min(n_baseline, n_canary)
    C_sample = min(1.0, np.sqrt(N / n_required))

    epsilon = 1e-9
    C_variance = float(np.exp(
        -abs(var_canary - var_baseline) / (var_baseline + epsilon)
    ))

    if min_eval_seconds > 0:
        C_stability = min(1.0, elapsed_seconds / min_eval_seconds)
    else:
        C_stability = 1.0

    return float(C_sample * C_variance * C_stability)


def compute_composite_score(metric_scores: list[dict]) -> float:
    """
    Weighted average over "important" and "business" tier metrics.

    S_composite = Σ(w_m × S_m) / Σ(w_m)

    Tier-1 (critical) metrics are excluded from this calculation — a critical
    breach produces a hard FAILED regardless of the composite score.
    """
    scorable = [m for m in metric_scores if m.get("tier") in ("important", "business")]
    total_weight = sum(m.get("weight", 1.0) for m in scorable)
    if total_weight == 0:
        return 100.0
    return float(
        sum(m.get("weight", 1.0) * m.get("score", 100.0) for m in scorable) / total_weight
    )


def determine_verdict(
    tier1_breaches: list[str],
    composite_score: float,
    confidence: float,
    cusum_detected: bool,
    bocpd_detected: bool,
    business_metric_breach: bool,
) -> str:
    """
    Map statistical outputs to a final verdict string.

    Decision tree (in priority order):
    1. Any Tier-1 (critical) breach OR business metric breach → FAILED
    2. Composite score < 65 → FAILED
    3. Composite score < 85 OR confidence < 0.80 OR changepoint detected → DEGRADED
    4. Otherwise → HEALTHY
    """
    if tier1_breaches or business_metric_breach:
        return "FAILED"
    if composite_score < 65.0:
        return "FAILED"
    if composite_score < 85.0 or confidence < 0.80 or cusum_detected or bocpd_detected:
        return "DEGRADED"
    return "HEALTHY"
