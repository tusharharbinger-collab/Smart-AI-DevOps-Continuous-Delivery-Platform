"""
services/verification-engine/src/tests_statistical/bocpd.py

Bayesian Online Change Point Detection (BOCPD).
Reference: Adams, R.P. & MacKay, D.J.C. (2007). arXiv:0710.3742.

Custom NumPy — deliberately NOT the `ruptures` library (ruptures is
offline/batch; this processes one observation at a time as the assignment
requires for streaming CPU/memory saturation signals).

Uses a Normal-Inverse-Gamma (NIG) conjugate prior for the predictive
distribution (Student-t marginal), enabling exact Bayesian updating without
MCMC.

Parameters
----------
hazard_lambda : float
    Expected run-length between change-points. λ=250 → ~1 CP per 250 obs.
change_threshold : float
    P(changepoint) threshold above which a CP is declared (default 0.85).
"""
import numpy as np
from scipy import stats


def run_bocpd(
    metric_series: np.ndarray,
    hazard_lambda: float = 250.0,
    change_threshold: float = 0.85,
) -> dict:
    """
    Run BOCPD on a univariate metric stream.

    Returns
    -------
    dict with keys:
      test                — "BOCPD"
      detected            — True if max_changepoint_prob > change_threshold
      max_changepoint_prob — peak CP probability seen
      change_index        — sample where CP was first declared, or None
      threshold           — the change_threshold used
    """
    T = len(metric_series)
    if T < 10:
        return {
            "test": "BOCPD", "detected": False,
            "max_changepoint_prob": 0.0, "change_index": None,
            "threshold": change_threshold, "note": "insufficient data (need ≥10)",
        }

    # Initialise NIG prior from first 5 observations
    init_mu = float(np.mean(metric_series[:5]))
    init_var = float(np.var(metric_series[:5])) + 1e-9
    kappa0, alpha0, beta0 = 1.0, 1.0, init_var

    # Run-length log-probabilities: log_R[r] = log P(run-length == r at time t)
    log_R = np.full(T + 1, -np.inf)
    log_R[0] = 0.0   # at t=0, run-length is 0 with probability 1

    mus = np.array([init_mu])
    kappas = np.array([kappa0])
    alphas = np.array([alpha0])
    betas = np.array([beta0])

    max_cp_prob = 0.0
    change_index: int | None = None

    for t in range(T):
        x_t = metric_series[t]

        # Predictive probability of x_t under each run-length hypothesis
        log_pred = _log_student_t(x_t, mus, kappas, alphas, betas)

        # Growth probability: existing run-lengths grow (no change-point)
        log_growth = log_R[:t + 1] + log_pred + np.log(1.0 - 1.0 / hazard_lambda)

        # Change-point probability: all run-lengths collapse to 0
        log_cp = float(np.logaddexp.reduce(log_R[:t + 1] + log_pred)) + np.log(1.0 / hazard_lambda)

        # Build new run-length distribution
        new_log_R = np.full(t + 2, -np.inf)
        new_log_R[1:t + 2] = log_growth
        new_log_R[0] = log_cp

        # Normalise
        log_norm = float(np.logaddexp.reduce(new_log_R[:t + 2]))
        log_R = new_log_R[:t + 2] - log_norm

        # Change-point probability (run-length = 0)
        cp_prob = float(np.exp(log_R[0]))
        if cp_prob > max_cp_prob:
            max_cp_prob = cp_prob
            if cp_prob > change_threshold and change_index is None:
                change_index = t

        # Update NIG parameters for each run-length
        mus, kappas, alphas, betas = _update_nig(x_t, mus, kappas, alphas, betas)
        # Prepend a new hypothesis for run-length 0 (fresh start after CP)
        mus = np.concatenate([[init_mu], mus])
        kappas = np.concatenate([[kappa0], kappas])
        alphas = np.concatenate([[alpha0], alphas])
        betas = np.concatenate([[beta0], betas])

    return {
        "test": "BOCPD",
        "detected": max_cp_prob > change_threshold,
        "max_changepoint_prob": float(max_cp_prob),
        "change_index": change_index,
        "threshold": change_threshold,
    }


def _log_student_t(
    x: float,
    mus: np.ndarray,
    kappas: np.ndarray,
    alphas: np.ndarray,
    betas: np.ndarray,
) -> np.ndarray:
    """Student-t predictive log-probability (marginal of NIG posterior)."""
    df = 2.0 * alphas
    scale = np.sqrt(betas * (kappas + 1.0) / (alphas * kappas))
    return stats.t.logpdf(x, df=df, loc=mus, scale=scale)


def _update_nig(
    x: float,
    mus: np.ndarray,
    kappas: np.ndarray,
    alphas: np.ndarray,
    betas: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Conjugate NIG posterior update for one observation."""
    kappas_new = kappas + 1.0
    mus_new = (kappas * mus + x) / kappas_new
    alphas_new = alphas + 0.5
    betas_new = betas + (kappas * (mus - x) ** 2) / (2.0 * kappas_new)
    return mus_new, kappas_new, alphas_new, betas_new
