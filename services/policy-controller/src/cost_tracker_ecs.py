"""
services/policy-controller/src/cost_tracker_ecs.py

The AWS ECS Fargate equivalent of cost_tracker.py. Real gap this closes:
controller.py has skipped cost-gating entirely for every
`deployment_target == "aws_ecs"` project since Module 8 landed (see
cost_tracker.py's own module docstring for the identical bug this already
fixed once on the Kubernetes side) — RULE 7 in
policies/delivery_guardrails.rego (`cost_delta_exceeds_limit`) could never
fire for an AWS project no matter how expensive a canary actually was, and
`cost_analysis` has had 0 rows for any AWS-deployed project since that
target existed.

Reads the LIVE baseline/canary ECS services' actual desiredCount + real
Fargate task-level cpu/memory straight from AWS (never a cached/stale
config) via `describe_current_container_config` — the same primitive
`graduate_canary_ecs` already trusts for the identical "never guess a
project's real resource sizing" reason — and computes a real compute-cost
delta using AWS's published on-demand Fargate Linux/x86 pricing:
$0.000011244/vCPU-second and $0.000001235/GB-second in us-east-1
(confirmed against https://aws.amazon.com/fargate/pricing/ on 2026-09-16;
override via env vars below if pricing changes or a different
region/architecture applies — this codebase already does the same
override-by-env-var thing for the Kubernetes-side estimate).

Reuses cost_tracker.py's own `_compute_cost_delta` formula (now
parameterized by rate) rather than a second copy of the arithmetic — ECS
and Kubernetes cost tracking both live inside policy-controller, so there
is no cross-service Docker-build-context boundary here the way there is
between this service and verification-engine (see cost_tracker.py's own
docstring for why THAT duplication is justified); duplicating the formula
again within the same service would just be two copies that could drift.
"""
import asyncio
import math
import os
from datetime import datetime, timedelta, timezone

import boto3
import structlog

from shared.aws_ecs_actuation import describe_current_container_config
from src.aws_actuation_executor import CLUSTER
from src.cost_tracker import _compute_cost_delta, compute_rightsizing_recommendation

logger = structlog.get_logger(__name__)

from shared.fargate_pricing import (
    FARGATE_CPU_COST_PER_VCPU_HOUR,
    FARGATE_MEM_COST_PER_GB_HOUR,
    _parse_fargate_cpu_vcpu,
    _parse_fargate_memory_gib,
)


def _read_ecs_service_footprint(ecs, cluster: str, service_name: str) -> dict | None:
    """
    Real desiredCount + real Fargate task-def cpu/memory for one ECS
    service — the ECS equivalent of cost_tracker.py's
    _read_deployment_footprint. Fail-soft: returns None (never raises) if
    the service doesn't exist (yet) or AWS isn't reachable, matching the
    Kubernetes side's exact contract.
    """
    try:
        resp = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
    except Exception as e:
        logger.warning("cost_tracker_ecs_describe_services_failed", service=service_name, error=str(e))
        return None

    active = [s for s in resp if s["status"] == "ACTIVE"]
    if not active:
        logger.warning("cost_tracker_ecs_service_not_found", service=service_name)
        return None

    desired_count = active[0]["desiredCount"]
    try:
        config = describe_current_container_config(ecs, cluster, service_name)
    except Exception as e:
        logger.warning("cost_tracker_ecs_task_def_read_failed", service=service_name, error=str(e))
        return None

    return {
        "desired_count": desired_count,
        "cpu_vcpu": _parse_fargate_cpu_vcpu(config["cpu"]),
        "mem_gib": _parse_fargate_memory_gib(config["memory"]),
    }


# ─────────────────────────── right-sizing (backlog #6) ───────────────────────────
#
# Connects two pieces that both existed but that nothing joined: real CloudWatch utilization
# (CPUUtilization / MemoryUtilization on the ECS service) -> compute_rightsizing_recommendation ->
# cost_analysis.rightsizing_rec, which the Cost tab already renders.
#
# Measured on the BASELINE service, deliberately not the canary: a canary serving 10% of traffic always
# looks over-provisioned, which would make every recommendation a false positive. It is ALWAYS only a
# recommendation - applying it stays gated behind a platform-admin approval (OPA Rule 8).

RIGHTSIZING_WINDOW_SECONDS = int(os.environ.get("RIGHTSIZING_WINDOW_SECONDS", "3600"))
# One-minute datapoints. Fewer than this and a p95 is noise, so no recommendation beats a made-up one.
RIGHTSIZING_MIN_SAMPLES = int(os.environ.get("RIGHTSIZING_MIN_SAMPLES", "10"))

# Fargate supported task sizes: cpu units -> allowed memory MiB. Anything else is rejected by ECS.
_FARGATE_SIZES = {
    256: [512, 1024, 2048],
    512: list(range(1024, 4097, 1024)),
    1024: list(range(2048, 8193, 1024)),
    2048: list(range(4096, 16385, 1024)),
    4096: list(range(8192, 30721, 1024)),
}


def _percentile(values, pct):
    """Nearest-rank percentile (no interpolation: a p95 should be a value that was actually observed)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def snap_to_fargate_size(cpu_vcpu, mem_gib):
    """
    The cheapest REAL Fargate size (cpu units + MiB) that covers the given need. The raw right-sizing
    formula can suggest 0.05 vCPU / 0.064 GiB, which cannot be deployed. None above 4 vCPU (not covered
    here rather than guessed).
    """
    need_cpu, need_mem = cpu_vcpu * 1024, mem_gib * 1024
    best = None
    for cpu_units, mems in _FARGATE_SIZES.items():
        if cpu_units < need_cpu:
            continue
        for mem_mib in mems:
            if mem_mib < need_mem:
                continue
            cost = (cpu_units / 1024) * FARGATE_CPU_COST_PER_VCPU_HOUR + (mem_mib / 1024) * FARGATE_MEM_COST_PER_GB_HOUR
            if best is None or cost < best[0]:
                best = (cost, cpu_units, mem_mib)
    if best is None:
        return None
    return {"cpu": best[1], "memory": best[2], "cpu_vcpu": best[1] / 1024, "mem_gib": best[2] / 1024}


def _fetch_utilization_percent(cw, cluster, service_name, metric, window_seconds):
    end = datetime.now(timezone.utc)
    resp = cw.get_metric_statistics(
        Namespace="AWS/ECS", MetricName=metric,
        Dimensions=[{"Name": "ClusterName", "Value": cluster}, {"Name": "ServiceName", "Value": service_name}],
        StartTime=end - timedelta(seconds=window_seconds), EndTime=end, Period=60, Statistics=["Average"],
    )
    return [dp["Average"] for dp in resp.get("Datapoints", [])]


def compute_ecs_rightsizing(region, cluster, service_name, footprint,
                            window_seconds=RIGHTSIZING_WINDOW_SECONDS, min_samples=RIGHTSIZING_MIN_SAMPLES, cloudwatch=None):
    """
    A right-sizing recommendation from real CloudWatch usage, or None when there is not enough evidence
    (too few datapoints, nothing running, CloudWatch unreachable). Fail-soft, never raises.
    `footprint` is _read_ecs_service_footprint's result for the measured service.
    """
    try:
        cw = cloudwatch or boto3.client("cloudwatch", region_name=region)
        cpu_pct = _fetch_utilization_percent(cw, cluster, service_name, "CPUUtilization", window_seconds)
        mem_pct = _fetch_utilization_percent(cw, cluster, service_name, "MemoryUtilization", window_seconds)
    except Exception as e:
        logger.warning("ecs_rightsizing_cloudwatch_failed", service=service_name, error=str(e))
        return None

    samples = min(len(cpu_pct), len(mem_pct))
    if samples < min_samples or footprint["cpu_vcpu"] <= 0 or footprint["mem_gib"] <= 0:
        logger.info("ecs_rightsizing_insufficient_data", service=service_name, samples=samples, needed=min_samples)
        return None

    cpu_p95, mem_p95 = _percentile(cpu_pct, 95), _percentile(mem_pct, 95)
    rec = compute_rightsizing_recommendation(
        observed_cpu_p95=cpu_p95 / 100 * footprint["cpu_vcpu"],
        observed_mem_p95=mem_p95 / 100 * footprint["mem_gib"],
        requested_cpu=footprint["cpu_vcpu"],
        requested_mem=footprint["mem_gib"],
    )
    snapped = snap_to_fargate_size(rec["recommended_cpu_vcpu"], rec["recommended_mem_gib"])
    rec.update({
        "source": "cloudwatch",
        "measured_service": service_name,
        "window_seconds": window_seconds,
        "sample_count": samples,
        "observed_cpu_p95_percent": round(cpu_p95, 1),
        "observed_mem_p95_percent": round(mem_p95, 1),
        "requested_cpu_vcpu": footprint["cpu_vcpu"],
        "requested_mem_gib": footprint["mem_gib"],
        # A concrete, deployable size - the raw recommended_* above can be e.g. 0.05 vCPU, which Fargate does not offer.
        "recommended_fargate_size": snapped,
        "caveat": "Measured on the baseline service over a short window: low utilization during a quiet period "
                  "looks like over-provisioning. Confirm against peak traffic before resizing.",
    })
    # Never call something over-provisioned when the smallest real size that fits is not smaller than what runs now.
    if snapped and snapped["cpu"] >= footprint["cpu_vcpu"] * 1024 and snapped["memory"] >= footprint["mem_gib"] * 1024:
        rec["is_overprovisioned"] = False
    return rec


async def compute_and_record_cost_ecs(
    pipeline_run_id: str,
    tenant_id: str | None,
    baseline_service_name: str,
    canary_service_name: str,
    region: str,
    db=None,
    max_permitted_delta_percent: float = 15.0,
) -> dict | None:
    """
    Fail-soft, mirrors compute_and_record_cost's exact contract: returns
    None (never raises) if AWS or either ECS service isn't reachable, so a
    project onboarded to AWS but not yet mid-rollout still runs — callers
    fall back to delta_percent=0.0 (never blocking), matching the
    Kubernetes side's established fail-soft convention.
    """
    try:
        ecs = boto3.client("ecs", region_name=region)
        baseline = _read_ecs_service_footprint(ecs, CLUSTER, baseline_service_name)
        canary = _read_ecs_service_footprint(ecs, CLUSTER, canary_service_name)
    except Exception as e:
        logger.warning("cost_tracker_ecs_unreachable", pipeline_run_id=pipeline_run_id, error=str(e))
        return None

    if baseline is None or canary is None:
        return None

    result = _compute_cost_delta(
        canary_replicas=canary["desired_count"],
        canary_cpu_vcpu=canary["cpu_vcpu"],
        canary_mem_gib=canary["mem_gib"],
        baseline_replicas=baseline["desired_count"],
        baseline_cpu_vcpu=baseline["cpu_vcpu"],
        baseline_mem_gib=baseline["mem_gib"],
        max_permitted_delta_percent=max_permitted_delta_percent,
        cpu_rate=FARGATE_CPU_COST_PER_VCPU_HOUR,
        mem_rate=FARGATE_MEM_COST_PER_GB_HOUR,
    )
    logger.info(
        "ecs_cost_delta_computed",
        pipeline_run_id=pipeline_run_id,
        delta_percent=result["delta_percent"],
        canary_cost_usd=result["canary_cost_usd"],
        baseline_cost_usd=result["baseline_cost_usd"],
    )

    # Right-sizing from real baseline usage (fail-soft: None when there is not enough data yet).
    rightsizing_rec = await asyncio.to_thread(compute_ecs_rightsizing, region, CLUSTER, baseline_service_name, baseline)

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
            logger.error("ecs_cost_analysis_db_write_failed", error=str(e), pipeline_run_id=pipeline_run_id)

    return result
