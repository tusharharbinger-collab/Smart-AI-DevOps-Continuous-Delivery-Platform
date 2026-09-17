"""
services/policy-controller/src/aws_actuation_executor.py

Module 8 continuation — the AWS ECS equivalent of `actuation_executor.py`,
mirroring its three functions 1:1 (same audit-recording, same idempotency
guarantees) so a project's real deploy target is invisible to everything
ABOVE this layer (controller.py/rollout_scheduler.py just call whichever
one `target["deployment_target"]` says to). The actual AWS mutation
primitives (`set_traffic_weights`, `register_task_definition`,
`ensure_service`) live in `shared/aws_ecs_actuation.py` — the one thing
both this module and pipeline-worker's onboarding/deploy code call, so
traffic-weight-shifting logic (safety-critical: it moves real production
traffic) has exactly one implementation, never two that could drift.

THE ONLY MODULE IN THIS SERVICE PERMITTED TO MUTATE AWS ECS STATE — same
invariant `actuation_executor.py` documents for Kubernetes, called
exclusively after opa_evaluator.py returns allow_action=True AND
verdict_verifier.py has confirmed the verdict's HMAC signature.
"""
import os

import boto3
import structlog

from shared.aws_ecs_actuation import set_traffic_weights, scale_service, describe_current_container_config, register_task_definition, ensure_service
from shared.live_url_check import verify_live_url
from shared.live_url_builder import build_live_url
from src.audit_writer import record_actuation

logger = structlog.get_logger(__name__)

CLUSTER = "smartcd-platform"
# Must match pipeline-worker's own AWS_ALB_BASE_URL exactly (the real
# shared ALB's stable DNS name) — see worker.py's own copy of this constant
# for why each service needs it independently rather than sharing one.
AWS_ALB_BASE_URL = os.environ.get("AWS_ALB_BASE_URL")


async def update_traffic_weights_ecs(
    pipeline_run_id: str,
    canary_weight: int,
    baseline_weight: int,
    authorized_by: str,
    service_name: str,
    path_prefix: str,
    region: str,
    tenant_id: str | None = None,
    db=None,
    verdict_status: str | None = None,
    confidence: float | None = None,
) -> dict:
    """AWS ECS equivalent of `actuation_executor.update_traffic_weights` —
    same audit-ledger contract, real ALB listener-rule weight change
    instead of an HTTPRoute JSON Patch."""
    set_traffic_weights(region, service_name, path_prefix, baseline_weight, canary_weight)
    logger.info(
        "ecs_traffic_weights_updated",
        pipeline_run_id=pipeline_run_id,
        canary_weight=canary_weight,
        baseline_weight=baseline_weight,
    )

    await record_actuation(
        pipeline_run_id=pipeline_run_id,
        action="WEIGHT_UPDATE",
        canary_weight=canary_weight,
        baseline_weight=baseline_weight,
        authorized_by=authorized_by,
        tenant_id=tenant_id,
        db=db,
        verdict=verdict_status,
        confidence=confidence,
    )
    return {"canary_weight": canary_weight, "baseline_weight": baseline_weight}


async def emergency_rollback_ecs(
    pipeline_run_id: str,
    authorized_by: str,
    service_name: str,
    path_prefix: str,
    region: str,
    tenant_id: str | None = None,
    db=None,
    verdict_status: str | None = None,
    confidence: float | None = None,
) -> dict:
    """Idempotent — safe to call twice. Sets weight to 0 AND scales the
    canary ECS service's desired count to 0, mirroring
    `actuation_executor.emergency_rollback`'s exact two-step shape."""
    
    ecs = boto3.client("ecs", region_name=region)
    # Post-cutover rollback: Ensure the baseline service is scaled up (if it was drained)
    # before we flip traffic back to it.
    try:
        scale_service(ecs, CLUSTER, f"{service_name}-baseline", desired_count=1)
    except Exception as e:
        logger.warning("ecs_scale_baseline_failed_during_rollback", error=str(e), pipeline_run_id=pipeline_run_id)

    await update_traffic_weights_ecs(
        pipeline_run_id,
        canary_weight=0,
        baseline_weight=100,
        authorized_by=authorized_by,
        service_name=service_name,
        path_prefix=path_prefix,
        region=region,
        tenant_id=tenant_id,
        db=db,
        verdict_status=verdict_status,
        confidence=confidence,
    )

    try:
        scale_service(ecs, CLUSTER, f"{service_name}-canary", desired_count=0)
        logger.info("ecs_emergency_rollback_complete", pipeline_run_id=pipeline_run_id)
    except Exception as e:
        logger.warning("ecs_scale_canary_to_zero_failed", error=str(e), pipeline_run_id=pipeline_run_id)

    await record_actuation(
        pipeline_run_id=pipeline_run_id,
        action="ROLLBACK",
        canary_weight=0,
        baseline_weight=100,
        authorized_by=authorized_by,
        tenant_id=tenant_id,
        db=db,
        verdict=verdict_status,
        confidence=confidence,
    )
    return {"status": "ROLLED_BACK"}


async def blue_green_cutover_ecs(
    pipeline_run_id: str,
    authorized_by: str,
    service_name: str,
    path_prefix: str,
    region: str,
    tenant_id: str | None = None,
    db=None,
    verdict_status: str | None = None,
    confidence: float | None = None,
) -> dict:
    """Atomic blue-green cutover for AWS ECS — sets canary weight to 100% and baseline to 0% instantly."""
    set_traffic_weights(region, service_name, path_prefix, baseline_weight=0, canary_weight=100)
    logger.info(
        "ecs_blue_green_cutover",
        pipeline_run_id=pipeline_run_id,
        service_name=service_name,
    )

    await record_actuation(
        pipeline_run_id=pipeline_run_id,
        action="BLUE_GREEN_CUTOVER",
        canary_weight=100,
        baseline_weight=0,
        authorized_by=authorized_by,
        tenant_id=tenant_id,
        db=db,
        verdict=verdict_status,
        confidence=confidence,
    )
    return {"status": "CUTOVER"}


async def graduate_canary_ecs(
    pipeline_run_id: str,
    authorized_by: str,
    service_name: str,
    path_prefix: str,
    region: str,
    tenant_id: str | None = None,
    db=None,
) -> dict:
    """
    AWS ECS equivalent of `actuation_executor.graduate_canary` — same real
    gap it closes (once a canary's ramp reaches 100%, nothing else makes
    that durable): reads the canary service's LIVE task definition (not
    whatever tag was originally requested — if it drifted, this graduates
    what's actually running), clones it onto the baseline service with only
    the image field changed (the ECS equivalent of the Kubernetes side's
    strategic merge patch — same "only change what needs to change"
    principle, via `describe_current_container_config` +
    `register_task_definition`), resets ALB weights to 100% baseline once
    both sides run the same image, and scales the canary service's desired
    count to 0 — ready to be reused as the next rollout's deploy target.
    Idempotent like `emergency_rollback_ecs`.
    """
    ecs = boto3.client("ecs", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)

    try:
        canary_config = describe_current_container_config(ecs, CLUSTER, f"{service_name}-canary")
    except Exception as e:
        logger.error("ecs_graduate_read_canary_failed", error=str(e), pipeline_run_id=pipeline_run_id)
        raise

    canary_image = canary_config["image"]
    image, _, image_tag = canary_image.rpartition(":")

    try:
        baseline_config = describe_current_container_config(ecs, CLUSTER, f"{service_name}-baseline")
        task_def_arn = register_task_definition(
            ecs, baseline_config["family"], canary_image, baseline_config["port"], baseline_config["cpu"],
            baseline_config["memory"], baseline_config["execution_role_arn"], baseline_config["log_group"],
            region, baseline_config["container_name"], "baseline", image_tag or "unknown",
            path_prefix=path_prefix,
        )
        target_group_name = f"{service_name}-baseline"[:32]
        target_group_arn = elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"][0]["TargetGroupArn"]
        ensure_service(
            ecs, CLUSTER, f"{service_name}-baseline", task_def_arn, target_group_arn,
            baseline_config["container_name"], baseline_config["port"],
            # Subnets/security group don't change on an in-place update
            # (ensure_service's create-vs-update branch only uses them on
            # first create) — updating an already-ACTIVE service ignores
            # both, matching ensure_service's own contract.
            [], "", desired_count=1,
        )
        logger.info(
            "ecs_baseline_image_graduated", pipeline_run_id=pipeline_run_id,
            service_name=service_name, new_image=canary_image,
        )
    except Exception as e:
        logger.error("ecs_graduate_baseline_update_failed", error=str(e), pipeline_run_id=pipeline_run_id)
        raise

    await update_traffic_weights_ecs(
        pipeline_run_id,
        canary_weight=0,
        baseline_weight=100,
        authorized_by=authorized_by,
        service_name=service_name,
        path_prefix=path_prefix,
        region=region,
        tenant_id=tenant_id,
        db=db,
    )

    # Guaranteed Live Web App CI/CD — real gap this closes: everything
    # above confirms the baseline runs the graduated image and shifts ALB
    # weight, but nothing ever confirmed a real visitor's request through
    # the real ALB actually gets a response — the same "healthy target
    # group, 404 through the real URL" class of bug the ALB path-prefix
    # trap describes (CLAUDE.md). Informational only, like the equivalent
    # first-deployment check in worker.py — a graduation that already
    # succeeded is never rolled back over this.
    if AWS_ALB_BASE_URL:
        verify_result = verify_live_url(build_live_url(AWS_ALB_BASE_URL, path_prefix))
        if db and tenant_id:
            try:
                await db.record_live_url_verification(tenant_id, pipeline_run_id, verify_result["verified"])
            except Exception as e:
                logger.error("live_url_verification_record_failed", error=str(e), pipeline_run_id=pipeline_run_id)
        logger.info(
            "ecs_graduate_live_url_verified" if verify_result["verified"] else "ecs_graduate_live_url_verification_failed",
            pipeline_run_id=pipeline_run_id, status_code=verify_result.get("status_code"), error=verify_result.get("error"),
        )

    try:
        scale_service(ecs, CLUSTER, f"{service_name}-canary", desired_count=0)
    except Exception as e:
        # Not fatal — the important safety-relevant change (baseline now
        # runs the graduated image, traffic is back on it) already
        # succeeded. An idle canary still holding old tasks just means the
        # next build reuses it without a fresh scale-up, which is safe.
        logger.warning("ecs_graduate_canary_scale_down_failed", error=str(e), pipeline_run_id=pipeline_run_id)

    await record_actuation(
        pipeline_run_id=pipeline_run_id,
        action="GRADUATE",
        authorized_by=authorized_by,
        tenant_id=tenant_id,
        db=db,
    )
    return {"status": "GRADUATED", "new_baseline_image": canary_image}
