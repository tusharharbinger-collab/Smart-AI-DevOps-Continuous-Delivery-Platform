"""
services/verification-engine/src/tests_statistical/business_metric_test.py

Contingency-table hypothesis test for binary/categorical business metrics
(e.g. payment success rate, checkout conversion).

This is the correct statistical tool for binary outcomes — Mann-Whitney is
for continuous distributions and is wrong here. See spec §6.4 rationale.

Automatically selects:
  - Fisher's exact test   when any expected cell count < 5 (small-sample
    regime where chi-square approximation is unreliable)
  - Chi-square test of independence (Yates continuity correction) otherwise

References
----------
Fisher, R.A. (1922). Proc. Royal Soc. Edinburgh, 42, 321–341.
Pearson, K. (1900). Philosophical Magazine 5(50):157–175.
"""
import numpy as np
from scipy import stats


def run_business_metric_test(
    baseline_success: int,
    baseline_total: int,
    canary_success: int,
    canary_total: int,
    alpha: float = 0.05,
    direction: str = "decrease_is_bad",   # conversion DROP is the regression to catch
) -> dict:
    """
    2×2 contingency table test for binary business metric regression.

    Parameters
    ----------
    baseline_success : int   — successes in baseline cohort
    baseline_total   : int   — total transactions in baseline cohort
    canary_success   : int   — successes in canary cohort
    canary_total     : int   — total transactions in canary cohort
    alpha            : float — significance level
    direction        : str   — "decrease_is_bad" (e.g. conversion rate)
                               "increase_is_bad" (e.g. error rate — prefer SPRT)

    Returns
    -------
    dict with keys:
      test                    — "Fisher's Exact Test" or "Chi-Square..."
      statistic               — odds-ratio (Fisher) or χ² (chi-square)
      p_value                 — two-sided p-value
      baseline_rate           — baseline conversion rate
      canary_rate             — canary conversion rate
      delta_percent           — (canary - baseline) / baseline × 100
      contingency_table       — [[bs, bf], [cs, cf]]
      is_significant          — p < alpha
      is_actionable_regression — significant AND in the bad direction
      used_fisher_exact       — True if Fisher's was chosen
    """
    if baseline_total == 0 or canary_total == 0:
        return {
            "test": "Business Metric (Contingency)",
            "detected": False,
            "is_significant": False,
            "is_actionable_regression": False,
            "note": "insufficient business-metric sample",
        }

    baseline_fail = baseline_total - baseline_success
    canary_fail = canary_total - canary_success

    table = np.array([
        [baseline_success, baseline_fail],
        [canary_success,   canary_fail],
    ])

    # Expected counts under independence — decides which test is valid
    row_totals = table.sum(axis=1)
    col_totals = table.sum(axis=0)
    grand_total = table.sum()
    expected = np.outer(row_totals, col_totals) / grand_total
    use_fisher = bool(np.any(expected < 5))

    baseline_rate = baseline_success / baseline_total
    canary_rate = canary_success / canary_total

    if use_fisher:
        odds_ratio, p_value = stats.fisher_exact(table, alternative="two-sided")
        test_name = "Fisher's Exact Test"
        statistic = float(odds_ratio)
    else:
        chi2, p_value, _dof, _expected = stats.chi2_contingency(table, correction=True)  # Yates
        test_name = "Chi-Square Test of Independence (Yates-corrected)"
        statistic = float(chi2)

    regression_direction_matches = (
        (direction == "decrease_is_bad" and canary_rate < baseline_rate) or
        (direction == "increase_is_bad" and canary_rate > baseline_rate)
    )

    delta_pct = None
    if baseline_rate > 0:
        delta_pct = round((canary_rate - baseline_rate) / baseline_rate * 100, 2)

    return {
        "test": test_name,
        "statistic": statistic,
        "p_value": float(p_value),
        "baseline_rate": round(baseline_rate, 6),
        "canary_rate": round(canary_rate, 6),
        "delta_percent": delta_pct,
        "contingency_table": table.tolist(),
        "is_significant": bool(p_value < alpha),
        "is_actionable_regression": bool(p_value < alpha and regression_direction_matches),
        "used_fisher_exact": use_fisher,
    }
