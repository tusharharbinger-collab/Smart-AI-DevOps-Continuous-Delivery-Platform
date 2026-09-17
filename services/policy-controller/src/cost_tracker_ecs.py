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
import os

import boto3
import structlog

from shared.aws_ecs_actuation import describe_current_container_config
from src.aws_actuation_executor import CLUSTER
from src.cost_tracker import _compute_cost_delta

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
            logger.error("ecs_cost_analysis_db_write_failed", error=str(e), pipeline_run_id=pipeline_run_id)

    return result
