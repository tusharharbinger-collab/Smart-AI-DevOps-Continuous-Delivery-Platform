"""
services/verification-engine/src/engine.py

Verification Engine dispatcher — routes each metric to its statistically
appropriate test by `category` (from the pipeline YAML, §4.1).

The routing is principled:
  error_rate    → SPRT Bernoulli LLR       (streaming, sequential)
  latency       → Mann-Whitney + KS        (distribution-free rank test)
  saturation    → CUSUM + BOCPD            (online change-point detection)
  business_metric → Fisher/Chi-square     (contingency table, binary outcome)

Multi-metric anomaly fusion is always applied via Isolation Forest across
all saturation vectors, supplementing per-metric tests.
"""
import numpy as np
import structlog
from datetime import datetime, timezone

from src.tests_statistical import (
    sprt_update, sprt_thresholds, SPRTState,
    run_mann_whitney,
    run_kolmogorov_smirnov,
    run_cusum,
    run_bocpd,
    run_business_metric_test,
    run_isolation_forest,
)
from src.preprocessing.iqr_filter import apply_iqr_filter
from src.scoring.confidence import compute_confidence, compute_composite_score, determine_verdict
from src.verdict import ImmutableVerdict

logger = structlog.get_logger(__name__)

# ── Dispatcher routing table ─────────────────────────────────────────────────
TEST_DISPATCH = {
    "error_rate":      "sprt",          # streaming Bernoulli — §6.1
    "latency":         "distribution",  # Mann-Whitney + KS — §6.2
    "saturation":      "changepoint",   # CUSUM + BOCPD — §6.3
    "business_metric": "contingency",   # Fisher/Chi-square — §6.4
}


class VerificationEngine:
    """
    Processes telemetry for one pipeline run and emits an ImmutableVerdict.

    Design constraints:
    - Does NOT import the `kubernetes` package (structural boundary, §8.1).
    - All decisions are based on statistical tests only — no static thresholds.
    """

    def run_verification(
        self,
        pipeline_run_id: str,
        metrics: list[dict],
        baseline_telemetry: dict,
        canary_telemetry: dict,
        elapsed_seconds: float,
        min_eval_seconds: float = 120.0,
    ) -> ImmutableVerdict:
        """
        Main entry point. Processes all metrics and produces a signed verdict.

        Parameters
        ----------
        pipeline_run_id   : str   Pipeline run UUID.
        metrics           : list[dict]  Metric configs from the pipeline YAML.
        baseline_telemetry: dict  {metric_name: raw_samples or count data}
        canary_telemetry  : dict  Same schema.
        elapsed_seconds   : float Seconds since canary was deployed.
        min_eval_seconds  : float Minimum evaluation window (from pipeline spec).
        """
        evidence: dict = {}
        tier1_breaches: list[str] = []
        metric_scores: list[dict] = []
        cusum_detected = False
        bocpd_detected = False
        business_metric_breach = False

        # Baseline arrays for Isolation Forest (collect saturation vectors)
        baseline_sat_rows: list[list[float]] = []
        canary_sat_rows: list[list[float]] = []

        for metric_cfg in metrics:
            name = metric_cfg["name"]
            category = metric_cfg.get("category", "latency")
            tier = metric_cfg.get("tier", "important")
            weight = float(metric_cfg.get("weight", 1.0))
            alpha = float(metric_cfg.get("alpha", 0.05))

            logger.info("running_metric_test", metric=name, category=category, tier=tier)

            try:
                result, score = self._dispatch(
                    name=name,
                    category=category,
                    alpha=alpha,
                    metric_cfg=metric_cfg,
                    baseline_telemetry=baseline_telemetry,
                    canary_telemetry=canary_telemetry,
                    baseline_sat_rows=baseline_sat_rows,
                    canary_sat_rows=canary_sat_rows,
                )
            except Exception as exc:
                logger.error("metric_test_failed", metric=name, error=str(exc))
                result = {"test": "ERROR", "note": str(exc)}
                score = 0.0   # treat as worst-case

            evidence[name] = result

            # Tier-1 (critical) breaches are hard FAILs
            if tier == "critical" and result.get("is_actionable_regression"):
                tier1_breaches.append(name)

            if category == "saturation":
                if result.get("cusum_detected"):
                    cusum_detected = True
                if result.get("bocpd_detected"):
                    bocpd_detected = True

            if category == "business_metric" and result.get("is_actionable_regression"):
                business_metric_breach = True

            metric_scores.append({"tier": tier, "weight": weight, "score": score})

        # Isolation Forest on all collected saturation vectors
        if baseline_sat_rows and canary_sat_rows:
            iforest_result = run_isolation_forest(
                np.array(baseline_sat_rows),
                np.array(canary_sat_rows),
            )
            evidence["_isolation_forest"] = iforest_result
            logger.info("isolation_forest", **{k: v for k, v in iforest_result.items() if k != "test"})

        # Collect sample counts from first latency metric for confidence
        n_baseline = n_canary = 0
        var_baseline = var_canary = 0.001
        for metric_cfg in metrics:
            name = metric_cfg["name"]
            if metric_cfg.get("category") == "latency" and name in baseline_telemetry:
                b = np.array(baseline_telemetry[name])
                c = np.array(canary_telemetry.get(name, []))
                if len(b) > 1 and len(c) > 1:
                    n_baseline, n_canary = len(b), len(c)
                    var_baseline, var_canary = float(np.var(b)), float(np.var(c))
                    break

        confidence = compute_confidence(
            n_baseline=n_baseline or 50,
            n_canary=n_canary or 50,
            var_baseline=var_baseline,
            var_canary=var_canary,
            elapsed_seconds=elapsed_seconds,
            min_eval_seconds=min_eval_seconds,
        )

        composite_score = compute_composite_score(metric_scores)
        status = determine_verdict(
            tier1_breaches=tier1_breaches,
            composite_score=composite_score,
            confidence=confidence,
            cusum_detected=cusum_detected,
            bocpd_detected=bocpd_detected,
            business_metric_breach=business_metric_breach,
        )

        verdict = ImmutableVerdict.build(
            pipeline_run_id=pipeline_run_id,
            status=status,
            composite_score=composite_score,
            confidence=confidence,
            evidence=evidence,
            tier1_breaches=tier1_breaches,
        )
        logger.info(
            "verdict_produced",
            pipeline_run_id=pipeline_run_id,
            status=status,
            confidence=confidence,
            composite_score=composite_score,
        )
        return verdict

    def _dispatch(
        self,
        name: str,
        category: str,
        alpha: float,
        metric_cfg: dict,
        baseline_telemetry: dict,
        canary_telemetry: dict,
        baseline_sat_rows: list,
        canary_sat_rows: list,
    ) -> tuple[dict, float]:
        """Route a single metric to its statistical test. Returns (result, score 0-100)."""

        if category == "error_rate":
            return self._run_sprt(name, metric_cfg, baseline_telemetry, canary_telemetry, alpha)

        elif category == "latency":
            return self._run_latency(name, baseline_telemetry, canary_telemetry, alpha)

        elif category == "saturation":
            return self._run_saturation(
                name, baseline_telemetry, canary_telemetry,
                baseline_sat_rows, canary_sat_rows
            )

        elif category == "business_metric":
            return self._run_business(name, metric_cfg, baseline_telemetry, canary_telemetry, alpha)

        else:
            logger.warning("unknown_metric_category", category=category, metric=name)
            return {"test": "SKIPPED", "reason": f"unknown category: {category}"}, 100.0

    def _run_sprt(self, name, cfg, baseline_tel, canary_tel, alpha) -> tuple[dict, float]:
        p0 = float(cfg.get("p0", 0.005))
        p1 = float(cfg.get("p1", 0.020))
        requests = canary_tel.get(f"{name}_requests", [])
        errors = canary_tel.get(f"{name}_errors", [])
        observations = [i < len(errors) and errors[i] for i in range(len(requests))]
        state = SPRTState()
        for obs in observations:
            state = sprt_update(state, bool(obs), p0=p0, p1=p1, alpha=alpha)
        thresholds = sprt_thresholds(alpha=alpha)
        score = 0.0 if state.decision == "REJECT_H0" else 100.0
        return {
            "test": "Wald SPRT", "decision": state.decision,
            "log_likelihood_ratio": state.log_likelihood_ratio,
            "total_requests": state.total_requests,
            "total_errors": state.total_errors,
            "thresholds": thresholds,
            "is_actionable_regression": state.decision == "REJECT_H0",
        }, score

    def _run_latency(self, name, baseline_tel, canary_tel, alpha) -> tuple[dict, float]:
        b = apply_iqr_filter(np.array(baseline_tel.get(name, [])))
        c = apply_iqr_filter(np.array(canary_tel.get(name, [])))
        if len(b) < 2 or len(c) < 2:
            return {"test": "Mann-Whitney U", "note": "insufficient samples"}, 100.0
        mw = run_mann_whitney(b, c, alpha=alpha)
        ks = run_kolmogorov_smirnov(b, c, alpha=alpha)
        score = 0.0 if mw["is_actionable_regression"] else (
            50.0 if mw["is_significant"] else 100.0
        )
        return {"mann_whitney": mw, "ks": ks, "is_actionable_regression": mw["is_actionable_regression"]}, score

    def _run_saturation(self, name, baseline_tel, canary_tel, bsl_rows, can_rows) -> tuple[dict, float]:
        b = np.array(baseline_tel.get(name, []))
        c = np.array(canary_tel.get(name, []))
        cusum_r = run_cusum(c, float(np.mean(b)) if len(b) > 0 else 0.5,
                            float(np.std(b)) if len(b) > 1 else 0.1)
        bocpd_r = run_bocpd(c)
        # Accumulate multi-dimensional rows for Isolation Forest
        if len(b) > 0:
            bsl_rows.extend([[float(v)] for v in b])
        if len(c) > 0:
            can_rows.extend([[float(v)] for v in c])
        detected = cusum_r["detected"] or bocpd_r["detected"]
        score = 30.0 if detected else 100.0
        return {
            "cusum": cusum_r, "bocpd": bocpd_r,
            "cusum_detected": cusum_r["detected"],
            "bocpd_detected": bocpd_r["detected"],
            "is_actionable_regression": detected,
        }, score

    def _run_business(self, name, cfg, baseline_tel, canary_tel, alpha) -> tuple[dict, float]:
        bs_succ = int(baseline_tel.get(f"{name}_success", 0))
        bs_total = int(baseline_tel.get(f"{name}_total", 0))
        can_succ = int(canary_tel.get(f"{name}_success", 0))
        can_total = int(canary_tel.get(f"{name}_total", 0))
        result = run_business_metric_test(bs_succ, bs_total, can_succ, can_total, alpha=alpha)
        score = 0.0 if result["is_actionable_regression"] else 100.0
        return result, score
