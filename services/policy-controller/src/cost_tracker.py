"""
services/policy-controller/src/cost_tracker.py

Real gap found live: RULE 7 in policies/delivery_guardrails.rego
(`cost_delta_exceeds_limit`) compares `input.cost_analysis.delta_percent`
against the pipeline's `maxPermittedCostDeltaPercent` guardrail, but
`handle_incoming_verdict()` (controller.py) always sent a hardcoded
`{"delta_percent": 0.0}` — so that guardrail could never fire regardless of
how much a canary rollout actually cost, and the `cost_analysis` Postgres
table (read by the per-deployment verification report and the periodic
delivery-health digest) has had 0 rows since day one.

This reads the LIVE baseline/canary Deployments' actual replica count and
CPU/memory requests straight from Kubernetes (never a cached/stale config,
so it can't drift from what's really running) and computes a real
compute-cost delta. The formula duplicates
`services/verification-engine/src/scoring/cost_analyzer.py::compute_cost_delta`
rather than importing across services — the two services have separate
Dockerfiles/dependency trees and no shared build step, and this is a small,
pure, already-unit-tested formula, so a second copy is cheaper than wiring
cross-service imports for one function.

No CPU/memory utilization telemetry is wired into this platform's
Prometheus queries yet (only error_rate/latency/saturation/business_metric
verification categories are), so `compute_rightsizing_recommendation`
(which needs observed p95 usage) is intentionally NOT called here —
right-sizing stays out of scope rather than fabricated from numbers nobody
actually measured.
"""
import os

import structlog
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

logger = structlog.get_logger(__name__)

CPU_COST_PER_VCPU_HOUR = float(os.environ.get("CPU_COST_PER_VCPU_HOUR", "0.0316"))
MEM_COST_PER_GIB_HOUR = float(os.environ.get("MEM_COST_PER_GIB_HOUR", "0.0042"))


def _load_kube():
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()


def _parse_cpu_vcpu(cpu_str: str | None) -> float:
    if not cpu_str:
        return 0.0
    cpu_str = cpu_str.strip()
    if cpu_str.endswith("m"):
        return float(cpu_str[:-1]) / 1000.0
    return float(cpu_str)


def _parse_memory_gib(mem_str: str | None) -> float:
    if not mem_str:
        return 0.0
    mem_str = mem_str.strip()
    binary_units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}
    decimal_units = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for suffix, bytes_per_unit in {**binary_units, **decimal_units}.items():
        if mem_str.endswith(suffix):
            return float(mem_str[: -len(suffix)]) * bytes_per_unit / (1024 ** 3)
    return float(mem_str) / (1024 ** 3)  # bare number = bytes


def _compute_cost_delta(
    canary_replicas: int,
    canary_cpu_vcpu: float,
    canary_mem_gib: float,
    baseline_replicas: int,
    baseline_cpu_vcpu: float,
    baseline_mem_gib: float,
    max_permitted_delta_percent: float,
    duration_hours: float = 1.0,
) -> dict:
    canary_cost = canary_replicas * (
        CPU_COST_PER_VCPU_HOUR * canary_cpu_vcpu + MEM_COST_PER_GIB_HOUR * canary_mem_gib
    ) * duration_hours
    baseline_cost = baseline_replicas * (
        CPU_COST_PER_VCPU_HOUR * baseline_cpu_vcpu + MEM_COST_PER_GIB_HOUR * baseline_mem_gib
    ) * duration_hours

    delta = canary_cost - baseline_cost
    delta_percent = (delta / baseline_cost * 100) if baseline_cost > 0 else 0.0

    return {
        "canary_cost_usd": round(canary_cost, 6),
        "baseline_cost_usd": round(baseline_cost, 6),
        "delta_usd": round(delta, 6),
        "delta_percent": round(delta_percent, 2),
        "exceeds_policy_limit": delta_percent > max_permitted_delta_percent,
    }


def _read_deployment_footprint(apps_v1, name: str, namespace: str) -> dict | None:
    try:
        deployment = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as e:
        logger.warning("cost_tracker_deployment_not_found", deployment=name, namespace=namespace, error=str(e))
        return None

    replicas = deployment.spec.replicas or 0
    containers = deployment.spec.template.spec.containers
    if not containers:
        return {"replicas": replicas, "cpu_vcpu": 0.0, "mem_gib": 0.0}

    requests = (containers[0].resources.requests or {}) if containers[0].resources else {}
    return {
        "replicas": replicas,
        "cpu_vcpu": _parse_cpu_vcpu(requests.get("cpu")),
        "mem_gib": _parse_memory_gib(requests.get("memory")),
    }


async def compute_and_record_cost(
    pipeline_run_id: str,
    tenant_id: str | None,
    namespace: str,
    baseline_deployment_name: str,
    canary_deployment_name: str,
    db=None,
    max_permitted_delta_percent: float = 15.0,
) -> dict | None:
    """
    Fail-soft: returns None (never raises) if the cluster or either
    Deployment isn't reachable — callers fall back to delta_percent=0.0
    (never blocking) so a demo/no-cluster pipeline still runs, matching the
    fail-soft convention this service already uses for alerting and RCA.
    """
    try:
        _load_kube()
        apps_v1 = k8s_client.AppsV1Api()
        baseline = _read_deployment_footprint(apps_v1, baseline_deployment_name, namespace)
        canary = _read_deployment_footprint(apps_v1, canary_deployment_name, namespace)
    except Exception as e:
        logger.warning("cost_tracker_kube_unreachable", pipeline_run_id=pipeline_run_id, error=str(e))
        return None

    if baseline is None or canary is None:
        return None

    result = _compute_cost_delta(
        canary_replicas=canary["replicas"],
        canary_cpu_vcpu=canary["cpu_vcpu"],
        canary_mem_gib=canary["mem_gib"],
        baseline_replicas=baseline["replicas"],
        baseline_cpu_vcpu=baseline["cpu_vcpu"],
        baseline_mem_gib=baseline["mem_gib"],
        max_permitted_delta_percent=max_permitted_delta_percent,
    )
    logger.info(
        "cost_delta_computed",
        pipeline_run_id=pipeline_run_id,
        delta_percent=result["delta_percent"],
        canary_cost_usd=result["canary_cost_usd"],
        baseline_cost_usd=result["baseline_cost_usd"],
    )

    if db and tenant_id:
        try:
            await db.record_cost_analysis(
                tenant_id=tenant_id,
                pipeline_run_id=pipeline_run_id,
                baseline_cost=result["baseline_cost_usd"],
                canary_cost=result["canary_cost_usd"],
                delta_percent=result["delta_percent"],
            )
        except Exception as e:
            logger.error("cost_analysis_db_write_failed", error=str(e), pipeline_run_id=pipeline_run_id)

    return result
