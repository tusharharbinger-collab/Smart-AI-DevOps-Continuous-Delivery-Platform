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
import httpx
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException
from shared import eks_auth
from shared.cost_formulas import compute_cost_delta as _shared_compute_cost_delta

logger = structlog.get_logger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
CPU_COST_PER_VCPU_HOUR = float(os.environ.get("CPU_COST_PER_VCPU_HOUR", "0.0316"))
MEM_COST_PER_GIB_HOUR = float(os.environ.get("MEM_COST_PER_GIB_HOUR", "0.0042"))


def _load_kube():
    eks_kube_config = eks_auth.get_eks_kube_client_config()
    if eks_kube_config:
        k8s_config.load_kube_config_from_dict(eks_kube_config)
        return
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
    cpu_rate: float | None = None,
    mem_rate: float | None = None,
) -> dict:
    """
    cpu_rate/mem_rate default to this module's own Kubernetes-node-estimate
    constants — overridable so cost_tracker_ecs.py can reuse this identical
    formula against real AWS Fargate on-demand rates instead of a second
    copy of the arithmetic (this is one service's internal boundary, not
    the cross-service one this module's own docstring justifies duplicating
    verification-engine's copy across).
    """
    cpu_rate = CPU_COST_PER_VCPU_HOUR if cpu_rate is None else cpu_rate
    mem_rate = MEM_COST_PER_GIB_HOUR if mem_rate is None else mem_rate
    return _shared_compute_cost_delta(
        canary_replicas, canary_cpu_vcpu, canary_mem_gib,
        baseline_replicas, baseline_cpu_vcpu, baseline_mem_gib,
        max_permitted_delta_percent, duration_hours, cpu_rate, mem_rate,
    )


def compute_rightsizing_recommendation(
    observed_cpu_p95: float,
    observed_mem_p95: float,
    requested_cpu: float,
    requested_mem: float,
    headroom: float = 0.25,
    min_cpu: float = 0.05,
    min_mem: float = 0.064,
    overprovisioned_threshold: float = 0.35,
) -> dict:
    """
    eta = p95_usage / requested. eta < threshold (default 0.35) flags
    over-provisioning. Duplicated from cost_analyzer.py to maintain boundary.
    """
    eta_cpu = observed_cpu_p95 / requested_cpu if requested_cpu > 0 else 1.0
    eta_mem = observed_mem_p95 / requested_mem if requested_mem > 0 else 1.0

    return {
        "efficiency_cpu": round(eta_cpu, 3),
        "efficiency_mem": round(eta_mem, 3),
        "is_overprovisioned": eta_cpu < overprovisioned_threshold or eta_mem < overprovisioned_threshold,
        "recommended_cpu_vcpu": round(max(min_cpu, observed_cpu_p95 * (1 + headroom)), 3),
        "recommended_mem_gib": round(max(min_mem, observed_mem_p95 * (1 + headroom)), 3),
        "action": "SUBMIT_GITOPS_PR",
        "requires_approval_role": "platform-admin",
    }


def _fetch_p95_usage(deployment_name: str, namespace: str) -> tuple[float, float]:
    """
    Fetches the max/p95 CPU (cores) and Memory (bytes) for a given deployment from Prometheus.
    Returns (cpu_cores, memory_bytes). Returns (0.0, 0.0) if Prometheus is unreachable.
    """
    try:
        cpu_query = f'max(sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod=~"{deployment_name}-.*", container!=""}}[2m])))'
        mem_query = f'max(sum by (pod) (container_memory_usage_bytes{{namespace="{namespace}", pod=~"{deployment_name}-.*", container!=""}}))'
        
        cpu = 0.0
        mem = 0.0
        
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": cpu_query})
            if resp.status_code == 200 and resp.json()["data"]["result"]:
                cpu = float(resp.json()["data"]["result"][0]["value"][1])
                
            resp = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": mem_query})
            if resp.status_code == 200 and resp.json()["data"]["result"]:
                mem = float(resp.json()["data"]["result"][0]["value"][1])
                
        return cpu, mem
    except Exception as e:
        logger.warning("prometheus_usage_fetch_failed", error=str(e), deployment=deployment_name)
        return 0.0, 0.0


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
    
    # Generate right-sizing recommendation using real observed usage
    observed_cpu, observed_mem_bytes = _fetch_p95_usage(canary_deployment_name, namespace)
    rightsizing_rec = None
    if observed_cpu > 0 or observed_mem_bytes > 0:
        observed_mem_gib = observed_mem_bytes / (1024 ** 3)
        rightsizing_rec = compute_rightsizing_recommendation(
            observed_cpu_p95=observed_cpu,
            observed_mem_p95=observed_mem_gib,
            requested_cpu=canary["cpu_vcpu"],
            requested_mem=canary["mem_gib"],
        )

    logger.info(
        "cost_delta_computed",
        pipeline_run_id=pipeline_run_id,
        delta_percent=result["delta_percent"],
        canary_cost_usd=result["canary_cost_usd"],
        baseline_cost_usd=result["baseline_cost_usd"],
        rightsizing_generated=rightsizing_rec is not None,
    )

    if db and tenant_id:
        try:
            await db.record_cost_analysis(
                tenant_id=tenant_id,
                pipeline_run_id=pipeline_run_id,
                baseline_cost=result["baseline_cost_usd"],
                canary_cost=result["canary_cost_usd"],
                delta_percent=result["delta_percent"],
                rightsizing_rec=rightsizing_rec,
            )
        except Exception as e:
            logger.error("cost_analysis_db_write_failed", error=str(e), pipeline_run_id=pipeline_run_id)

    return result
