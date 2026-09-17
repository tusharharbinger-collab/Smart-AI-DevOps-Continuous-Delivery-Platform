"""
services/pipeline-worker/src/aws/ecs_deploy_task.py

Module 8 continuation — real gap found live while proving AWS end to end:
`onboard_ecs_service` (ecs_onboarding.py) only ever ran ONCE, at project
creation, using whatever image:tag the wizard was given at that moment.
Every subsequent `git push` triggers a real pipeline run whose `build`
stage genuinely builds and pushes a NEW image tag to ECR — but nothing
told the already-running ECS canary service about it. This is the ECS
equivalent of `deploy_task.py::deploy_project_canary_task`: called from
worker.py's `deploy` stage dispatch on every rollout, not just the first
one, so a project's real, ongoing commits actually reach AWS.

`_deploy_ecs_cohort` mirrors `actuation_executor.py::graduate_canary`'s own
"only change the image field" principle: it reads the cohort's CURRENTLY
running task definition (port, cpu, memory, execution role, log group,
container name — all fixed at onboarding time) and clones it with only the
image swapped, rather than requiring worker.py to thread port/cpu/memory
through the pipeline YAML just to register an equivalent task definition.

`set_first_deployment_ecs_weights` mirrors
`deploy_task.py::set_first_deployment_route_weights` exactly (including
the real OPA freeze-window check via the same policy rule) — the ECS
equivalent of cutting a genuinely first-ever deployment over to 100%
without a canary comparison, since there is no prior version to compare
against yet.
"""
import os
from datetime import datetime, timezone

import boto3
import structlog

from shared.aws_ecs_actuation import (
    register_task_definition,
    ensure_service,
    set_traffic_weights,
    wait_for_service_stable,
    describe_current_container_config,
    scale_service,
)
from shared.cost_formulas import compute_cost_delta
from shared.opa_client import evaluate_policy_sync
from src.aws.ecs_onboarding import _clients, _default_vpc_public_subnets

logger = structlog.get_logger(__name__)

from shared.fargate_pricing import (
    FARGATE_CPU_COST_PER_VCPU_HOUR,
    FARGATE_MEM_COST_PER_GB_HOUR,
)

# Must match policy-controller/src/aws_actuation_executor.py::CLUSTER exactly
# — duplicated as a plain constant for the same reason SHARED_ALB_NAME is
# duplicated in shared/aws_ecs_actuation.py: separate Docker build contexts,
# no shared Python package boundary except `shared/` itself, and a fixed
# platform-wide cluster name is safe to duplicate since it's never computed.
CLUSTER = "smartcd-platform"


def _lookup_security_group_id(ec2, name: str, vpc_id: str) -> str:
    """
    The task security group is created once at onboarding
    (`ensure_task_security_group`) — every subsequent deploy just needs to
    find it again, never create a second one for the same service.
    """
    existing = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc_id]}]
    )["SecurityGroups"]
    if not existing:
        raise RuntimeError(
            f"Task security group '{name}' not found — this project was never onboarded to AWS, "
            "or onboarding didn't complete. Re-run onboarding before deploying."
        )
    return existing[0]["GroupId"]


def _deploy_ecs_cohort(
    pipeline_run_id: str, service_name: str, cohort: str, image: str, image_tag: str, region: str,
    cluster: str = "smartcd-platform",
) -> dict:
    """
    Registers a new task-definition revision with the freshly-built image
    (everything else — port/cpu/memory/execution role/log group/container
    name — copied verbatim from the cohort's currently running task
    definition, the ECS equivalent of a Kubernetes strategic-merge patch
    that only touches `image`) and updates the cohort's ECS service in
    place. Does NOT touch traffic weights — that stays the verdict-gated
    actuation path's job (policy-controller), exactly like the Kubernetes
    `deploy` stage never touches the HTTPRoute either.
    """
    c = _clients(region)
    ecs_service_name = f"{service_name}-{cohort}"
    current = describe_current_container_config(c["ecs"], cluster, ecs_service_name)

    vpc_id, subnet_ids = _default_vpc_public_subnets(c["ec2"])
    task_sg_id = _lookup_security_group_id(c["ec2"], f"{service_name}-sg", vpc_id)
    target_group_name = f"{service_name}-{cohort}"[:32]
    target_group_arn = c["elbv2"].describe_target_groups(Names=[target_group_name])["TargetGroups"][0]["TargetGroupArn"]

    task_def_arn = register_task_definition(
        c["ecs"], current["family"], f"{image}:{image_tag}", current["port"], current["cpu"], current["memory"],
        current["execution_role_arn"], current["log_group"], region, current["container_name"], cohort, image_tag,
        path_prefix=current.get("path_prefix"),
    )
    result = ensure_service(
        c["ecs"], cluster, ecs_service_name, task_def_arn, target_group_arn,
        current["container_name"], current["port"], subnet_ids, task_sg_id, desired_count=1,
    )
    logger.info(
        "ecs_cohort_deployed",
        pipeline_run_id=pipeline_run_id,
        service_name=service_name,
        cohort=cohort,
        image_tag=image_tag,
        result=result["status"],
    )
    return result


def deploy_ecs_canary_task(pipeline_run_id: str, service_name: str, image: str, image_tag: str, region: str) -> dict:
    return _deploy_ecs_cohort(pipeline_run_id, service_name, "canary", image, image_tag, region)


def deploy_ecs_baseline_task(pipeline_run_id: str, service_name: str, image: str, image_tag: str, region: str) -> dict:
    return _deploy_ecs_cohort(pipeline_run_id, service_name, "baseline", image, image_tag, region)


def wait_for_ecs_service_ready(region: str, service_name: str, cohort: str, cluster: str = "smartcd-platform") -> dict:
    return wait_for_service_stable(region, cluster, f"{service_name}-{cohort}")


def set_first_deployment_ecs_weights(
    pipeline_run_id: str, service_name: str, path_prefix: str, region: str, pipeline_policy: dict
) -> dict:
    """
    Real gap found live: mirrors
    `deploy_task.py::set_first_deployment_route_weights` exactly — a
    project's genuinely first-ever AWS deployment has no prior version to
    compare against, so it ships straight to 100% (both ECS services
    already carry the identical image at this point; which "side" nominally
    holds traffic is cosmetic, matching the K8s side's own reasoning). Still
    real-guardrail-checked: freeze windows are the one guardrail that
    meaningfully applies even with zero statistical evidence, checked here
    via the SAME OPA policy (Rule 9, "FIRST_DEPLOYMENT") — not a second,
    Python-side copy of the day/time comparison.
    """
    now = datetime.now(timezone.utc)
    opa_result = evaluate_policy_sync(
        {
            "requested_action": "FIRST_DEPLOYMENT",
            "pipeline_policy": pipeline_policy,
            "runtime_context": {
                "cluster_maintenance_lock": False,
                "current_day": now.strftime("%A"),
                "current_time": now.strftime("%H:%M"),
            },
        }
    )
    if not opa_result["allow_action"]:
        raise RuntimeError(f"First deployment blocked by policy: {opa_result.get('rejection_reasons')}")

    result = set_traffic_weights(region, service_name, path_prefix, baseline_weight=100, canary_weight=0)
    logger.info("ecs_first_deployment_weights_set", pipeline_run_id=pipeline_run_id, service_name=service_name)
    return result


def cutover_blue_green_ecs_weights(
    pipeline_run_id: str, service_name: str, path_prefix: str, region: str, pipeline_policy: dict
) -> dict:
    """
    Blue-green's atomic cutover — real gap this closes: the previously
    wired blue-green path (policy-controller's controller.py, OPA's RULE 10
    "BLUE_GREEN_CUTOVER") only ever fired after a real statistical verdict
    reached the final canary step at 100% traffic weight, which a genuinely
    new web app with zero real visitors can never produce — it would sit
    DEGRADED ("insufficient samples") forever and never go live. This is
    the health-gated replacement: called only after the caller (worker.py's
    blue-green rollout branch) has already confirmed real infrastructure
    health — wait_for_ecs_service_ready (task stability) AND
    wait_for_target_group_healthy (real ALB HTTP liveness) — so no
    statistical evidence is needed or requested. Still real-guardrail-
    checked: freeze windows, via OPA's HEALTH_GATED_CUTOVER rule, the same
    "the one guardrail that still meaningfully applies with zero
    statistical evidence" reasoning set_first_deployment_ecs_weights above
    already uses for FIRST_DEPLOYMENT.
    """
    now = datetime.now(timezone.utc)
    opa_result = evaluate_policy_sync(
        {
            "requested_action": "HEALTH_GATED_CUTOVER",
            "pipeline_policy": pipeline_policy,
            "runtime_context": {
                "cluster_maintenance_lock": False,
                "current_day": now.strftime("%A"),
                "current_time": now.strftime("%H:%M"),
            },
        }
    )
    if not opa_result["allow_action"]:
        raise RuntimeError(f"Blue-green cutover blocked by policy: {opa_result.get('rejection_reasons')}")

    result = set_traffic_weights(region, service_name, path_prefix, baseline_weight=0, canary_weight=100)
    logger.info("ecs_blue_green_cutover_weights_set", pipeline_run_id=pipeline_run_id, service_name=service_name)
    return result


def rollback_blue_green_ecs_weights(pipeline_run_id: str, service_name: str, path_prefix: str, region: str) -> dict:
    """
    Post-cutover automatic rollback — real gap this closes: an atomic
    cutover can still fail AFTER the weight flip (the new version passes
    ECS task-stability and ALB target-group health but is still broken in a
    way only a real request through the ALB's path-prefix routing reveals
    — see CLAUDE.md's ALB-path-prefix trap). Reverting to the baseline
    (still running the previously-live, never-touched image) needs no OPA
    gate — rolling back to an already-proven-healthy version is never a
    decision that should be policy-blockable, matching
    emergency_rollback_ecs's own reasoning on the policy-controller side.
    """
    result = set_traffic_weights(region, service_name, path_prefix, baseline_weight=100, canary_weight=0)
    logger.warning("ecs_blue_green_cutover_rolled_back", pipeline_run_id=pipeline_run_id, service_name=service_name)
    return result


def _parse_fargate_cpu_vcpu(cpu_str: str | None) -> float:
    """Fargate task-level `cpu` is CPU units as a plain numeric string — 1024 units = 1 vCPU."""
    if not cpu_str:
        return 0.0
    return float(cpu_str) / 1024.0


def _parse_fargate_memory_gib(memory_str: str | None) -> float:
    """Fargate task-level `memory` is MiB as a plain numeric string."""
    if not memory_str:
        return 0.0
    return float(memory_str) / 1024.0


def _read_ecs_service_footprint(ecs, cluster: str, service_name: str) -> dict | None:
    """Mirrors policy-controller's src/cost_tracker_ecs.py::_read_ecs_service_footprint
    exactly (same fail-soft contract) — reimplemented rather than cross-
    imported, since pipeline-worker has no access to policy-controller's
    private module (separate Docker build context)."""
    try:
        resp = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
    except Exception as e:
        logger.warning("blue_green_cost_describe_services_failed", service=service_name, error=str(e))
        return None

    active = [s for s in resp if s["status"] == "ACTIVE"]
    if not active:
        return None

    desired_count = active[0]["desiredCount"]
    try:
        config = describe_current_container_config(ecs, cluster, service_name)
    except Exception as e:
        logger.warning("blue_green_cost_task_def_read_failed", service=service_name, error=str(e))
        return None

    return {
        "desired_count": desired_count,
        "cpu_vcpu": _parse_fargate_cpu_vcpu(config["cpu"]),
        "mem_gib": _parse_fargate_memory_gib(config["memory"]),
    }


def compute_blue_green_cost(
    service_name: str, region: str, max_permitted_delta_percent: float = 15.0
) -> dict | None:
    """
    Real gap found live (2026-09-17): cost_analysis has had 0 rows for any
    blue-green rollout on any project, ever — RULE 7 in
    policies/delivery_guardrails.rego and the Reports & Cost UI both read
    from a table only the verdict-driven controller.py ever wrote to, which
    blue-green's health-gated path never reaches. Reads the LIVE baseline/
    canary ECS services' real desiredCount + Fargate task cpu/memory (never
    a cached/stale config) right after cutover, when canary is genuinely at
    100% and baseline is about to be replaced — the one moment in a
    blue-green rollout where "what does this canary actually cost" is a
    meaningful snapshot. Fail-soft: returns None (never raises) if AWS
    isn't reachable, matching compute_and_record_cost_ecs's own contract —
    a cost snapshot failing must never fail the actual rollout.
    """
    try:
        ecs = boto3.client("ecs", region_name=region)
        baseline = _read_ecs_service_footprint(ecs, CLUSTER, f"{service_name}-baseline")
        canary = _read_ecs_service_footprint(ecs, CLUSTER, f"{service_name}-canary")
    except Exception as e:
        logger.warning("blue_green_cost_unreachable", service_name=service_name, error=str(e))
        return None

    if baseline is None or canary is None:
        return None

    result = compute_cost_delta(
        canary_replicas=canary["desired_count"],
        canary_cpu_vcpu=canary["cpu_vcpu"],
        canary_mem_gib=canary["mem_gib"],
        baseline_replicas=baseline["desired_count"],
        baseline_cpu_vcpu=baseline["cpu_vcpu"],
        baseline_mem_gib=baseline["mem_gib"],
        max_permitted_delta_percent=max_permitted_delta_percent,
        duration_hours=1.0,
        cpu_rate=FARGATE_CPU_COST_PER_VCPU_HOUR,
        mem_rate=FARGATE_MEM_COST_PER_GB_HOUR,
    )
    logger.info("blue_green_cost_computed", service_name=service_name, delta_percent=result["delta_percent"])
    return result


def graduate_blue_green_ecs(
    pipeline_run_id: str, service_name: str, image: str, image_tag: str, path_prefix: str, region: str,
) -> dict:
    """
    Makes a confirmed-healthy blue-green cutover durable — real gap this
    closes: without this, the NEXT rollout's `canary_deploy` stage would
    overwrite the canary service (still holding the just-promoted image)
    before this run's win was ever made permanent, and the baseline service
    would silently keep running the OLD image forever even though 100% of
    traffic has moved on. Mirrors `graduate_canary_ecs`'s own "only the
    image field changes" principle (via `deploy_ecs_baseline_task`, the
    same strategic-merge-patch pattern `_deploy_ecs_cohort` already uses) —
    deliberately reimplemented here rather than calling into
    policy-controller's private module, since pipeline-worker has no access
    to it (separate Docker build context); every operation used below is
    already a `shared/` primitive both services call independently, so
    there is still exactly one implementation of each underlying AWS
    mutation, just two thin call sites.
    """
    deploy_ecs_baseline_task(pipeline_run_id, service_name, image, image_tag, region)
    wait_for_ecs_service_ready(region, service_name, "baseline")
    result = set_traffic_weights(region, service_name, path_prefix, baseline_weight=100, canary_weight=0)
    try:
        c = _clients(region)
        scale_service(c["ecs"], CLUSTER, f"{service_name}-canary", desired_count=0)
    except Exception as e:
        # Not fatal — the safety-relevant change (baseline durably runs the
        # new image, traffic is on a stable steady state) already
        # succeeded. An idle canary still holding old tasks just means the
        # next rollout's deploy stage overwrites it without a fresh
        # scale-up, which is safe. Mirrors graduate_canary_ecs's identical
        # non-fatal handling of this same step.
        logger.warning("ecs_blue_green_graduate_canary_scale_down_failed", error=str(e), pipeline_run_id=pipeline_run_id)
    logger.info("ecs_blue_green_graduated", pipeline_run_id=pipeline_run_id, service_name=service_name, image=f"{image}:{image_tag}")
    return result
