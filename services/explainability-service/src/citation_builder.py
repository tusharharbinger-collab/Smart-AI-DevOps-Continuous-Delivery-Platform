"""
services/explainability-service/src/citation_builder.py

Converts raw statistical test outputs (produced by verification-engine's
tests_statistical/* modules, see engine.py's `evidence` dict) into a single,
precise, human-readable citation sentence per metric. Every promote/rollback
decision embeds one of these per contributing metric — this is what makes a
decision "grounded" rather than an assertion. Spec §9.1.
"""


def cite_mann_whitney(metric_name: str, result: dict) -> str:
    return (
        f"{metric_name}: canary median {result['canary_median'] * 1000:.0f}ms "
        f"vs baseline {result['baseline_median'] * 1000:.0f}ms, "
        f"Mann-Whitney p={result['p_value']:.4g}, CLES={result['effect_size_cles']:.3f}"
    )


def cite_sprt(metric_name: str, state: dict, thresholds: tuple[float, float]) -> str:
    A, B = thresholds
    llr = state["log_likelihood_ratio"]
    decision = state.get("decision", "CONTINUE")
    if decision == "REJECT_H0":
        verdict_clause = f"SPRT LLR={llr:.2f} exceeded upper bound {A:.2f}"
    elif decision == "ACCEPT_H0":
        verdict_clause = f"SPRT LLR={llr:.2f} fell below lower bound {B:.2f}"
    else:
        verdict_clause = f"SPRT LLR={llr:.2f} within bounds [{B:.2f}, {A:.2f}]"
    return (
        f"{metric_name}: {state['total_errors']}/{state['total_requests']} errors, "
        f"{verdict_clause}"
    )


def cite_business_metric(metric_name: str, result: dict) -> str:
    return (
        f"{metric_name}: canary {result['canary_rate'] * 100:.2f}% "
        f"vs baseline {result['baseline_rate'] * 100:.2f}% "
        f"({result['test']}, p={result['p_value']:.4g})"
    )


def cite_bocpd(metric_name: str, result: dict) -> str:
    if not result.get("detected"):
        return (
            f"{metric_name}: no structural change point detected "
            f"(max P={result['max_changepoint_prob']:.3f})"
        )
    return (
        f"{metric_name}: change point detected at sample {result['change_index']} "
        f"(P={result['max_changepoint_prob']:.3f} > threshold {result['threshold']})"
    )


def cite_cusum(metric_name: str, result: dict) -> str:
    if not result.get("detected"):
        return f"{metric_name}: CUSUM found no sustained drift (S+={result['final_S_pos']:.2f}, S-={result['final_S_neg']:.2f})"
    direction = "upward" if result.get("breach_positive") else "downward"
    return (
        f"{metric_name}: CUSUM detected a sustained {direction} shift at sample "
        f"{result['breach_index']}"
    )


def cite_isolation_forest(result: dict) -> str:
    return (
        f"multi-metric saturation profile: Isolation Forest mean anomaly score "
        f"{result['mean_anomaly_score']:.3f} "
        f"({'ABOVE' if result.get('detected') else 'below'} threshold {result['threshold']})"
    )


def build_full_citation(all_metric_results: list[dict]) -> str:
    """
    Assembles the complete grounded-explanation string for a decision, joining
    every contributing metric's citation with a semicolon. This exact string
    is stored on the ImmutableVerdict.evidence and surfaced verbatim in the
    UI's EvidencePanel component (§11.2).

    Each entry in `all_metric_results` is expected to carry:
      {"metric_name": str, "test_type": str, "result": dict, "thresholds"?: tuple}
    """
    citations = []
    for m in all_metric_results:
        test_type = m.get("test_type")
        name = m.get("metric_name", "metric")
        result = m.get("result", {})
        try:
            if test_type == "mann_whitney":
                citations.append(cite_mann_whitney(name, result))
            elif test_type == "sprt":
                citations.append(cite_sprt(name, result, m["thresholds"]))
            elif test_type == "business_metric":
                citations.append(cite_business_metric(name, result))
            elif test_type == "bocpd":
                citations.append(cite_bocpd(name, result))
            elif test_type == "cusum":
                citations.append(cite_cusum(name, result))
            elif test_type == "isolation_forest":
                citations.append(cite_isolation_forest(result))
        except (KeyError, TypeError):
            # Missing/malformed evidence must degrade the citation, never crash
            # the explanation pipeline (the rollback/promotion has already happened).
            citations.append(f"{name}: evidence incomplete, citation unavailable")
    return "; ".join(citations)


def build_citations_from_engine_evidence(evidence: dict) -> str:
    """
    Adapter for verification-engine's actual `evidence` dict shape (engine.py),
    which nests results under keys like {"mann_whitney": {...}, "ks": {...}}
    for latency metrics, or a flat business-metric-test result dict, rather
    than the {"test_type": ..., "result": ...} envelope `build_full_citation`
    expects. This walks that native shape directly.
    """
    entries: list[dict] = []
    for metric_name, payload in evidence.items():
        if metric_name == "_isolation_forest":
            entries.append({"metric_name": metric_name, "test_type": "isolation_forest", "result": payload})
            continue
        if not isinstance(payload, dict):
            continue

        if "mann_whitney" in payload:
            entries.append({"metric_name": metric_name, "test_type": "mann_whitney", "result": payload["mann_whitney"]})
        elif "cusum" in payload and "bocpd" in payload:
            entries.append({"metric_name": metric_name, "test_type": "cusum", "result": payload["cusum"]})
            entries.append({"metric_name": metric_name, "test_type": "bocpd", "result": payload["bocpd"]})
        elif payload.get("test") == "Wald SPRT":
            entries.append({
                "metric_name": metric_name,
                "test_type": "sprt",
                "result": payload,
                "thresholds": tuple(payload.get("thresholds", (0.0, 0.0))),
            })
        elif "used_fisher_exact" in payload:
            entries.append({"metric_name": metric_name, "test_type": "business_metric", "result": payload})

    return build_full_citation(entries)
