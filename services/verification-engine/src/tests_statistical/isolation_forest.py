"""
services/verification-engine/src/tests_statistical/isolation_forest.py

Multi-metric anomaly scoring via Isolation Forest.
Reference: Liu, F.T., Ting, K.M. & Zhou, Z.H. (2008). ICDM 2008.

Trains on baseline's multivariate saturation vectors; scores canary
vectors against that learned "normal" manifold. This fuses cpu, memory,
disk_io, and gc_pause signals into a single anomaly score — no single
metric alone triggers this test.

The 0-to-1 normalized score is a supplementary signal used in the
composite scorer alongside the per-metric test results.
"""
import numpy as np
from sklearn.ensemble import IsolationForest


def run_isolation_forest(
    baseline_saturation: np.ndarray,   # shape (n, m): m features per sample
    canary_saturation: np.ndarray,
    contamination: float = 0.05,
    anomaly_score_threshold: float = 0.65,
) -> dict:
    """
    Train Isolation Forest on baseline, score canary.

    Parameters
    ----------
    baseline_saturation : np.ndarray  shape (n_baseline, n_features)
        Multi-dimensional baseline saturation vectors,
        e.g. columns = [cpu_util, mem_util, disk_io, gc_pause_ms]
    canary_saturation   : np.ndarray  shape (n_canary, n_features)
        Same feature schema for canary.
    contamination       : float       Estimated fraction of outliers in baseline.
    anomaly_score_threshold : float   Normalized score above which a vector is anomalous.

    Returns
    -------
    dict with keys:
      test                — "Isolation Forest"
      mean_anomaly_score  — mean normalized score across canary vectors ∈ [0, 1]
      max_anomaly_score   — worst-case canary vector score
      detected            — mean_anomaly_score ≥ anomaly_score_threshold
      anomalous_vectors   — count of canary vectors above threshold
      threshold           — the anomaly_score_threshold used
    """
    if len(baseline_saturation) < 10 or len(canary_saturation) < 5:
        return {
            "test": "Isolation Forest",
            "detected": False,
            "mean_anomaly_score": 0.0,
            "max_anomaly_score": 0.0,
            "anomalous_vectors": 0,
            "threshold": anomaly_score_threshold,
            "note": "insufficient data (baseline ≥10, canary ≥5 required)",
        }

    clf = IsolationForest(
        n_estimators=100,
        max_samples=min(256, len(baseline_saturation)),
        contamination=contamination,
        random_state=42,
    )
    clf.fit(baseline_saturation)

    # score_samples returns negative anomaly scores; negate to get positive ones
    raw_scores = clf.score_samples(canary_saturation)
    anomaly_scores = -raw_scores   # higher = more anomalous

    # Normalize to [0, 1] for interpretable reporting
    span = anomaly_scores.max() - anomaly_scores.min()
    if span > 0:
        normalized = (anomaly_scores - anomaly_scores.min()) / span
    else:
        # If all scores are identical, map to 1.0 if anomalous (>0.5 in raw -score_samples space) else 0.0
        normalized = np.ones_like(anomaly_scores) if (len(anomaly_scores) > 0 and anomaly_scores[0] > 0.5) else np.zeros_like(anomaly_scores)

    mean_score = float(np.mean(normalized))
    max_score = float(np.max(normalized))
    n_anomalous = int(np.sum(normalized >= anomaly_score_threshold))

    return {
        "test": "Isolation Forest",
        "mean_anomaly_score": round(mean_score, 4),
        "max_anomaly_score": round(max_score, 4),
        "detected": mean_score >= anomaly_score_threshold,
        "anomalous_vectors": n_anomalous,
        "threshold": anomaly_score_threshold,
    }
