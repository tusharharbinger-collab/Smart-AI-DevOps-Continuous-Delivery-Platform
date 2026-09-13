"""
services/pipeline-worker/src/tasks/verification_task.py

Executes verification for the active canary step. Prefers real telemetry:
if the pipeline's `verificationConfig.metrics` entries carry a `prometheus`
query block (see docs/roadmap/02-real-telemetry.md), this asks
verification-engine's `/verify` to pull real, live-scraped data itself
(`use_prometheus=True`) rather than sending any samples at all. Only when
NO metric declares a `prometheus` block does this fall back to synthesizing
representative telemetry locally — an explicit, clearly-logged demo/fallback
path, not the default, for pipelines that haven't been wired to a real
metrics source yet.
"""
import os
import random

import requests
import structlog

logger = structlog.get_logger(__name__)

VERIFICATION_ENGINE_URL = os.environ.get(
    "VERIFICATION_ENGINE_URL", "http://verification-engine:8002"
)


def _synthesize_telemetry(metric_cfg: dict, n_samples: int = 200) -> tuple[dict, dict]:
    """
    FALLBACK ONLY — used when a metric has no `prometheus` query block
    configured. Generates baseline/canary telemetry drawn from a stable,
    healthy distribution (canary statistically indistinguishable from
    baseline); a stand-in so an unwired pipeline still has something
    concrete to verify against, never a substitute for real telemetry once
    a pipeline is wired to a real metrics source.
    """
    name = metric_cfg["name"]
    category = metric_cfg.get("category", "latency")
    baseline: dict = {}
    canary: dict = {}

    if category == "latency":
        baseline[name] = [max(0.001, random.gauss(0.042, 0.006)) for _ in range(n_samples)]
        canary[name] = [max(0.001, random.gauss(0.042, 0.006)) for _ in range(n_samples)]
    elif category == "error_rate":
        baseline[f"{name}_requests"] = list(range(n_samples))
        baseline[f"{name}_errors"] = [random.random() < 0.003 for _ in range(n_samples)]
        canary[f"{name}_requests"] = list(range(n_samples))
        canary[f"{name}_errors"] = [random.random() < 0.003 for _ in range(n_samples)]
    elif category == "saturation":
        baseline[name] = [max(0.0, random.gauss(0.45, 0.03)) for _ in range(n_samples)]
        canary[name] = [max(0.0, random.gauss(0.45, 0.03)) for _ in range(n_samples)]
    elif category == "business_metric":
        baseline[f"{name}_total"] = n_samples
        baseline[f"{name}_success"] = int(n_samples * 0.98)
        canary[f"{name}_total"] = n_samples
        canary[f"{name}_success"] = int(n_samples * 0.975)

    return baseline, canary


def run_verification_task(
    pipeline_run_id: str,
    verification_config: dict | None = None,
    elapsed_seconds: float = 180.0,
    trace_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """
    Calls verification-engine's /verify — with real Prometheus-backed
    telemetry when the pipeline's metrics are wired for it, synthesized
    telemetry otherwise — and returns the resulting (signed, published)
    verdict.
    """
    logger.info("verification_task_started", pipeline_run_id=pipeline_run_id)

    metrics = (verification_config or {}).get("metrics", [])
    min_eval_seconds = (verification_config or {}).get("minEvaluationWindowSeconds", 120)

    use_prometheus = any(m.get("prometheus") for m in metrics)
    baseline_telemetry: dict = {}
    canary_telemetry: dict = {}

    if use_prometheus:
        logger.info("verification_using_real_prometheus_telemetry", pipeline_run_id=pipeline_run_id)
    else:
        logger.warning(
            "verification_using_synthetic_fallback_telemetry",
            pipeline_run_id=pipeline_run_id,
            reason="no metric in verificationConfig declares a `prometheus` query block",
        )
        for metric_cfg in metrics:
            b, c = _synthesize_telemetry(metric_cfg)
            baseline_telemetry.update(b)
            canary_telemetry.update(c)

    try:
        resp = requests.post(
            f"{VERIFICATION_ENGINE_URL}/verify",
            json={
                "pipeline_run_id": pipeline_run_id,
                "metrics": metrics,
                "use_prometheus": use_prometheus,
                "baseline_telemetry": baseline_telemetry,
                "canary_telemetry": canary_telemetry,
                "elapsed_seconds": max(elapsed_seconds, min_eval_seconds),
                "min_eval_seconds": min_eval_seconds,
                "trace_id": trace_id,
                "tenant_id": tenant_id,
            },
            headers={"X-Trace-Id": trace_id} if trace_id else {},
            timeout=30,
        )
        resp.raise_for_status()
        verdict = resp.json()
        logger.info(
            "verification_task_completed",
            pipeline_run_id=pipeline_run_id,
            status=verdict.get("status"),
        )
        return verdict
    except requests.RequestException as e:
        logger.error("verification_task_failed", pipeline_run_id=pipeline_run_id, error=str(e))
        return {"status": "UNVERIFIABLE", "pipeline_run_id": pipeline_run_id, "note": str(e)}
