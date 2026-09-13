"""
services/explainability-service/tests/test_citation_builder.py

Spec §9.1 / Phase 7 verification: proves the citation strings contain the
exact numeric facts they claim to cite.
"""
from src.citation_builder import (
    build_citations_from_engine_evidence,
    build_full_citation,
    cite_business_metric,
    cite_cusum,
    cite_mann_whitney,
    cite_sprt,
)


def test_mann_whitney_citation_format():
    result = {"canary_median": 0.245, "baseline_median": 0.112, "p_value": 0.002, "effect_size_cles": 0.81}
    citation = cite_mann_whitney("p95_latency", result)
    assert "245ms" in citation and "112ms" in citation and "p=0.002" in citation


def test_sprt_citation_format():
    state = {"log_likelihood_ratio": 4.12, "total_errors": 15, "total_requests": 300, "decision": "REJECT_H0"}
    citation = cite_sprt("http_error_rate", state, thresholds=(2.89, -2.29))
    assert "LLR=4.12" in citation and "exceeded upper bound 2.89" in citation


def test_sprt_citation_continue_state():
    state = {"log_likelihood_ratio": 0.5, "total_errors": 2, "total_requests": 300, "decision": "CONTINUE"}
    citation = cite_sprt("http_error_rate", state, thresholds=(2.89, -2.29))
    assert "within bounds" in citation


def test_business_metric_citation_format():
    result = {
        "test": "Chi-Square Test of Independence (Yates-corrected)",
        "canary_rate": 0.85, "baseline_rate": 0.98, "p_value": 0.0001,
    }
    citation = cite_business_metric("checkout_success_rate", result)
    assert "85.00%" in citation and "98.00%" in citation and "p=0.0001" in citation


def test_cusum_citation_detected():
    result = {"detected": True, "breach_positive": True, "breach_negative": False, "breach_index": 42}
    citation = cite_cusum("cpu_saturation", result)
    assert "upward" in citation and "42" in citation


def test_build_full_citation_joins_multiple_metrics():
    entries = [
        {"metric_name": "p95_latency", "test_type": "mann_whitney",
         "result": {"canary_median": 0.245, "baseline_median": 0.112, "p_value": 0.002, "effect_size_cles": 0.81}},
        {"metric_name": "http_error_rate", "test_type": "sprt",
         "result": {"log_likelihood_ratio": 4.12, "total_errors": 15, "total_requests": 300, "decision": "REJECT_H0"},
         "thresholds": (2.89, -2.29)},
    ]
    citation = build_full_citation(entries)
    assert "; " in citation
    assert "p95_latency" in citation and "http_error_rate" in citation


def test_build_citations_from_real_engine_evidence_shape():
    """
    Matches the ACTUAL shape produced by verification-engine/src/engine.py's
    `evidence` dict (nested {"mann_whitney": ..., "ks": ...} for latency,
    {"cusum": ..., "bocpd": ...} for saturation, flat dict with "test":
    "Wald SPRT" for error_rate, and a flat business-metric-test result).
    """
    evidence = {
        "p95_latency_seconds": {
            "mann_whitney": {
                "canary_median": 0.245, "baseline_median": 0.112,
                "p_value": 0.002, "effect_size_cles": 0.81,
            },
            "ks": {"p_value": 0.01},
            "is_actionable_regression": True,
        },
        "http_error_rate": {
            "test": "Wald SPRT", "decision": "REJECT_H0",
            "log_likelihood_ratio": 4.12, "total_requests": 300, "total_errors": 15,
            "thresholds": (2.89, -2.29),
            "is_actionable_regression": True,
        },
        "cpu_saturation": {
            "cusum": {"detected": True, "breach_positive": True, "breach_negative": False,
                      "breach_index": 10, "final_S_pos": 6.0, "final_S_neg": 0.0},
            "bocpd": {"detected": False, "max_changepoint_prob": 0.2, "change_index": None},
            "cusum_detected": True, "bocpd_detected": False,
            "is_actionable_regression": True,
        },
        "checkout_success_rate": {
            "test": "Chi-Square Test of Independence (Yates-corrected)",
            "statistic": 55.2, "p_value": 0.0001,
            "baseline_rate": 0.98, "canary_rate": 0.85,
            "is_significant": True, "is_actionable_regression": True,
            "used_fisher_exact": False,
        },
        "_isolation_forest": {
            "test": "Isolation Forest", "mean_anomaly_score": 0.72,
            "max_anomaly_score": 0.9, "detected": True, "threshold": 0.65,
        },
    }
    citation = build_citations_from_engine_evidence(evidence)
    assert "p95_latency_seconds" in citation
    assert "LLR=4.12" in citation
    assert "cpu_saturation" in citation
    assert "checkout_success_rate" in citation
    assert "Isolation Forest" in citation
