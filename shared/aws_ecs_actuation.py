"""
shared/aws_ecs_actuation.py

Module 8 continuation — real AWS ECS actuation primitives shared by BOTH
pipeline-worker (initial onboarding + every subsequent build's canary
deploy) and policy-controller (ongoing weight-shift/graduate/rollback on
every verdict). These two services are separate Docker images with no
shared Python package boundary except this `shared/` directory (mounted
read-only into every container, exactly like `eks_auth.py`) — so this is
the one place this logic can live without either service importing the
other's source tree, which isn't structurally possible (different build
contexts).

Deliberately NOT duplicated into each service: traffic-weight shifting and
graduation are safety-critical (they move real production traffic and real
spend) — this codebase already treats that category of logic as needing
exactly one source of truth (verdict signing, OPA evaluation), never two
copies that could drift. Moved here verbatim from
`services/pipeline-worker/src/aws/ecs_onboarding.py` (that module now
imports these instead of defining its own copies) — pure extraction, no
behavior change.
"""
import time

import boto3
import structlog

logger = structlog.get_logger(__name__)

# Must match services/pipeline-worker/src/aws/ecs_manifest.py::SHARED_ALB_NAME
# exactly — duplicated as a plain constant (not imported) because that
# module lives in pipeline-worker's own src/ tree, which policy-controller's
# Docker image does not have on its filesystem at all (separate build
# context). A single hardcoded platform-wide ALB name is safe to duplicate
# this way since it's a fixed string, never computed logic.
SHARED_ALB_NAME = "smartcd-platform-alb"


def register_task_definition(
    ecs, family: str, image: str, port: int, cpu: str, memory: str, execution_role_arn: str,
    log_group: str, region: str, container_name: str, cohort: str, app_version: str,
    path_prefix: str | None = None,
) -> str:
    resp = ecs.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu=cpu,
        memory=memory,
        executionRoleArn=execution_role_arn,
        containerDefinitions=[
            {
                "name": container_name,
                "image": image,
                "portMappings": [{"containerPort": port, "protocol": "tcp"}],
                "essential": True,
                # Matches the Kubernetes side's own DEPLOYMENT_COHORT/
                # APP_VERSION env vars exactly (deploy_canary_task.py) —
                # same observable shape regardless of which real target a
                # project runs on.
                # PATH_PREFIX carries the ALB's per-project routing prefix into
                # the container so synthesized nginx/static containers can strip
                # it internally via alias.
                "environment": [
                    {"name": "DEPLOYMENT_COHORT", "value": cohort},
                    {"name": "APP_VERSION", "value": app_version},
                    {"name": "PATH_PREFIX", "value": path_prefix or ""},
                    # Real gap found live (2026-09-18): a project declares its
                    # real listen port in the wizard, and that port is
                    # already used to configure the ALB target group's
                    # health check and this task's own portMapping — but it
                    # was never actually told to the CONTAINER. Any app using
                    # the extremely common `process.env.PORT || <default>`
                    # idiom (Node/Express, most PaaS-style apps) silently
                    # listened on its own hardcoded default instead,
                    # mismatching whatever port ECS/the ALB actually expect —
                    # confirmed live: a real app's own container logs showed
                    # it running on 3000 while the target group health-
                    # checked port 8080, causing a real, repeated
                    # "Task failed ELB health checks" cycle with no code bug
                    # on the app's side at all.
                    {"name": "PORT", "value": str(port)},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": cohort,
                        "awslogs-create-group": "true",
                    },
                },
            }
        ],
    )
    task_def_arn = resp["taskDefinition"]["taskDefinitionArn"]
    logger.info("ecs_task_definition_registered", family=family, arn=task_def_arn, image=image)
    return task_def_arn


def ensure_service(
    ecs, cluster: str, service_name: str, task_def_arn: str, target_group_arn: str,
    container_name: str, port: int, subnet_ids: list[str], security_group_id: str, desired_count: int,
) -> dict:
    """
    Creates the ECS service on first onboarding, or updates it in place
    (new task definition revision, same service) on every subsequent
    deploy — mirrors `deploy_project_canary_task`'s own create-or-patch
    idempotency on the Kubernetes side.
    """
    existing = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
    active = [s for s in existing if s["status"] == "ACTIVE"]
    if active:
        ecs.update_service(cluster=cluster, service=service_name, taskDefinition=task_def_arn, desiredCount=desired_count)
        logger.info("ecs_service_updated", service=service_name, task_def=task_def_arn)
        return {"status": "updated", "service_name": service_name}

    ecs.create_service(
        cluster=cluster,
        serviceName=service_name,
        taskDefinition=task_def_arn,
        desiredCount=desired_count,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [security_group_id],
                "assignPublicIp": "ENABLED",
            }
        },
        loadBalancers=[
            {"targetGroupArn": target_group_arn, "containerName": container_name, "containerPort": port}
        ],
        healthCheckGracePeriodSeconds=30,
    )
    logger.info("ecs_service_created", service=service_name, task_def=task_def_arn)
    return {"status": "created", "service_name": service_name}


def set_traffic_weights(region: str, service_name: str, path_prefix: str, baseline_weight: int, canary_weight: int) -> dict:
    """
    Real weighted traffic shift for an ALREADY-onboarded project — the ECS
    equivalent of `actuation_executor.py`'s HTTPRoute weight patch on the
    Kubernetes side. Finds this project's existing listener rule by its
    path_prefix (the same match key `ensure_listener_rule` used to create
    it) and updates only the weights, leaving the rule's target groups
    untouched.
    """
    elbv2 = boto3.client("elbv2", region_name=region)
    albs = elbv2.describe_load_balancers(Names=[SHARED_ALB_NAME])["LoadBalancers"]
    if not albs:
        raise RuntimeError("Shared ALB 'smartcd-platform-alb' not found — has this project been onboarded to AWS yet?")
    listeners = elbv2.describe_listeners(LoadBalancerArn=albs[0]["LoadBalancerArn"])["Listeners"]
    listener = next((l for l in listeners if l["Port"] == 80), None)
    if not listener:
        raise RuntimeError("Shared ALB has no HTTP:80 listener.")

    rules = elbv2.describe_rules(ListenerArn=listener["ListenerArn"])["Rules"]
    rule = next(
        (r for r in rules if r.get("Conditions") and r["Conditions"][0].get("Values") == [f"{path_prefix}*"]),
        None,
    )
    if not rule:
        raise RuntimeError(f"No listener rule found for path_prefix '{path_prefix}' — has {service_name} been onboarded?")

    # Real bug found live (2026-09-15): this used to read the EXISTING rule's
    # own TargetGroups array and assume position 0 is always baseline,
    # position 1 always canary — but the ELBv2 API does not guarantee it
    # echoes back target groups in the same order they were submitted in.
    # Caught only because a 100/0 (never 50/50) weight split makes a
    # position swap catastrophic and immediately observable: a real
    # graduation inverted the weights, sending 100% of traffic to the
    # canary target group at the exact moment its ECS service was scaled to
    # zero — every request would have 503'd. Fixed by identifying each
    # target group by its real ARN (looked up by the exact naming
    # convention `ecs_manifest.py`/`_deploy_ecs_cohort` already use — never
    # by array position) rather than trusting readback order.
    baseline_tg_name = f"{service_name}-baseline"[:32]
    canary_tg_name = f"{service_name}-canary"[:32]
    try:
        baseline_tg_arn = elbv2.describe_target_groups(Names=[baseline_tg_name])["TargetGroups"][0]["TargetGroupArn"]
        canary_tg_arn = elbv2.describe_target_groups(Names=[canary_tg_name])["TargetGroups"][0]["TargetGroupArn"]
    except elbv2.exceptions.TargetGroupNotFoundException:
        raise RuntimeError(
            f"Baseline/canary target groups for '{service_name}' not found — has this project been onboarded to AWS yet?"
        )

    new_action = {
        "Type": "forward",
        "ForwardConfig": {
            "TargetGroups": [
                {"TargetGroupArn": baseline_tg_arn, "Weight": baseline_weight},
                {"TargetGroupArn": canary_tg_arn, "Weight": canary_weight},
            ]
        },
    }
    elbv2.modify_rule(RuleArn=rule["RuleArn"], Actions=[new_action])
    logger.info(
        "ecs_traffic_weights_set", service_name=service_name, baseline_weight=baseline_weight, canary_weight=canary_weight
    )
    return {"status": "weights_updated", "baseline_weight": baseline_weight, "canary_weight": canary_weight}


def wait_for_service_stable(region: str, cluster: str, service_name: str, timeout_seconds: int = 180) -> dict:
    """
    Real liveness gate for ECS — the direct equivalent of
    `wait_for_deployment_ready` on the Kubernetes side. Polls the real ECS
    service until `runningCount` meets `desiredCount`, or raises after
    timeout. A task that never passes its health check (bad image, crashing
    container, misconfigured health_check_path) must never be treated as
    "deployed."
    """
    ecs = boto3.client("ecs", region_name=region)
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        resp = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
        if not resp:
            raise RuntimeError(f"ECS service '{service_name}' not found in cluster '{cluster}'.")
        svc = resp[0]
        running = svc["runningCount"]
        desired = svc["desiredCount"]
        last_status = {"running": running, "desired": desired}
        if running >= desired and desired > 0:
            logger.info("ecs_service_stable", service_name=service_name, **last_status)
            return {"ready": True, **last_status}
        time.sleep(5)
    raise RuntimeError(
        f"ECS service '{service_name}' did not stabilize within {timeout_seconds}s "
        f"(running={last_status['running'] if last_status else 0}/{last_status['desired'] if last_status else '?'}) — "
        f"check task stopped-reason/CloudWatch Logs for the real cause before retrying."
    )


def describe_current_container_config(ecs, cluster: str, service_name: str) -> dict:
    """
    Real gap this closes: `graduate_canary_ecs` (policy-controller) needs to
    clone the BASELINE task definition with only the image field swapped —
    the exact "strategic merge patch, only the image changes" principle
    `actuation_executor.py::graduate_canary` already uses on the Kubernetes
    side. Reads the service's live task definition and returns everything
    `register_task_definition` needs to re-register an equivalent revision:
    family, image, port, cpu, memory, execution role, log group, container
    name — so a graduation never has to guess or hardcode any of a
    project's real resource sizing.
    """
    svc = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
    if not svc:
        raise RuntimeError(f"ECS service '{service_name}' not found in cluster '{cluster}'.")
    task_def_arn = svc[0]["taskDefinition"]
    task_def = ecs.describe_task_definition(taskDefinition=task_def_arn)["taskDefinition"]
    container = task_def["containerDefinitions"][0]
    log_options = container.get("logConfiguration", {}).get("options", {})
    env_vars = {e.get("name"): e.get("value") for e in container.get("environment", [])}
    path_prefix = env_vars.get("PATH_PREFIX", "")
    return {
        "family": task_def["family"],
        "image": container["image"],
        "port": container["portMappings"][0]["containerPort"],
        "cpu": task_def["cpu"],
        "memory": task_def["memory"],
        "execution_role_arn": task_def["executionRoleArn"],
        "log_group": log_options.get("awslogs-group", f"/ecs/{service_name}"),
        "container_name": container["name"],
        "path_prefix": path_prefix,
    }


def scale_service(ecs, cluster: str, service_name: str, desired_count: int) -> None:
    """Idempotent — setting the same desiredCount twice is a no-op change,
    matching this platform's existing actuation idempotency discipline."""
    ecs.update_service(cluster=cluster, service=service_name, desiredCount=desired_count)
    logger.info("ecs_service_scaled", service_name=service_name, desired_count=desired_count)


def _get_current_deployment_task_private_ips(ecs, cluster: str, service_name: str) -> list[str]:
    """
    Real gap this closes: during an ECS rolling deployment, the OLD task
    from the previous deployment stays registered (state `draining`, not yet
    fully deregistered) alongside the NEW task while ECS confirms the new
    one is healthy — the service's target group can carry both at once for
    a real, non-trivial window. Identifying "the deployment's own task(s)"
    by which task definition revision is currently PRIMARY (not just
    "whatever's running") is what lets the health/live-URL gate tell the
    difference between the new task and a stale one still winding down.
    Returns an empty list (not an error) when the new task hasn't
    registered an ENI yet — the caller's poll loop treats that as "not
    ready", not a failure.
    """
    services = ecs.describe_services(cluster=cluster, services=[service_name])["services"]
    if not services:
        return []
    primary = next((d for d in services[0]["deployments"] if d["status"] == "PRIMARY"), None)
    if primary is None:
        return []
    current_task_def_arn = primary["taskDefinition"]

    task_arns = ecs.list_tasks(cluster=cluster, serviceName=service_name, desiredStatus="RUNNING")["taskArns"]
    if not task_arns:
        return []
    tasks = ecs.describe_tasks(cluster=cluster, tasks=task_arns)["tasks"]

    ips: list[str] = []
    for task in tasks:
        if task["taskDefinitionArn"] != current_task_def_arn:
            continue
        for attachment in task.get("attachments", []):
            for detail in attachment.get("details", []):
                if detail.get("name") == "privateIPv4Address":
                    ips.append(detail["value"])
    return ips


def wait_for_target_group_healthy(
    region: str,
    target_group_name: str,
    cluster: str,
    service_name: str,
    timeout_seconds: int = 180,
    poll_interval_seconds: float = 5.0,
) -> dict:
    """
    Real ALB-driven HTTP health check — the direct target-group-level
    equivalent of `wait_for_service_stable`'s ECS-level check above, and the
    "HTTP liveness probe" half of blue-green's guaranteed-cutover gate. The
    ALB itself performs the real GET against the target's declared
    `health_check_path`.

    Real gap found live (2026-09-17), confirmed by a deliberate
    break-and-drill against a real project: this used to require EVERY
    currently-registered target to be healthy, which is wrong two ways at
    once. (1) A still-draining OLD target from the previous deployment can
    never become "healthy" again — it's on its way out — so a fast enough
    redeploy could make this wait forever on a target that was never going
    to recover, timing out even though the new task was genuinely fine. (2)
    The far more dangerous direction: if the OLD target was still the ONLY
    one registered as "healthy" at poll time (the new task hadn't finished
    ENI/health-check registration yet), `all(...)` was trivially true —
    this function reported "healthy" without the new deployment's task
    having been checked AT ALL, which is exactly how a real drill this
    session got a broken app cut into 100% production traffic: the
    post-cutover live-URL check ran 2 seconds after cutover and happened to
    hit the still-healthy old task, not the new broken one, so rollback
    never fired.

    Fixed by identifying the SPECIFIC task(s) belonging to the service's
    current (PRIMARY) deployment via `_get_current_deployment_task_private_ips`
    and requiring THOSE targets — matched by private IP — to report
    healthy. If the new task hasn't registered a target yet, that's "not
    ready", not a failure of the old target's state.

    Second real gap found live in the SAME drill, after fixing the above:
    confirming the new task's OWN health-check path is healthy isn't
    enough on its own — an OLD target from the previous deployment stays
    fully "InService" (not just draining) for AWS's target-group
    `deregistration_delay` (default 300s; `ensure_target_group` now sets
    this to 30s at creation for exactly this reason), and the ALB
    round-robins real requests across BOTH targets during that whole
    window. A caller doing a real end-to-end check right after this
    function returns (e.g. `verify_live_url` hitting the actual app path,
    not just the health-check path) can still land on the stale old task
    by pure chance and get a false "it's fine" reading — which is exactly
    what let a deliberately-broken app go live undetected even after the
    first fix above. So this now ALSO waits for every OTHER target (any
    target whose IP isn't the new deployment's own) to be gone from the
    target group entirely — not just draining — before declaring healthy,
    so a live traffic check run immediately after this returns is
    guaranteed to reach the new task, never a leftover old one.
    """
    elbv2 = boto3.client("elbv2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    tg_name = target_group_name[:32]
    try:
        target_group_arn = elbv2.describe_target_groups(Names=[tg_name])["TargetGroups"][0]["TargetGroupArn"]
    except elbv2.exceptions.TargetGroupNotFoundException:
        raise RuntimeError(f"Target group '{tg_name}' not found — has this project been onboarded to AWS yet?")

    deadline = time.monotonic() + timeout_seconds
    last_states: list[dict] = []
    last_new_task_ips: list[str] = []
    while time.monotonic() < deadline:
        last_new_task_ips = _get_current_deployment_task_private_ips(ecs, cluster, service_name)
        descriptions = elbv2.describe_target_health(TargetGroupArn=target_group_arn)["TargetHealthDescriptions"]
        all_states = [
            {"target": d["Target"]["Id"], "state": d["TargetHealth"]["State"], "reason": d["TargetHealth"].get("Reason")}
            for d in descriptions
        ]
        last_states = all_states
        matched = [s for s in all_states if s["target"] in last_new_task_ips]
        stray = [s for s in all_states if s["target"] not in last_new_task_ips]
        if last_new_task_ips and matched and all(s["state"] == "healthy" for s in matched) and not stray:
            logger.info(
                "ecs_target_group_healthy", target_group_name=tg_name, new_task_targets=matched, all_targets=all_states
            )
            return {"healthy": True, "targets": matched}
        time.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Target group '{tg_name}' did not confirm the new deployment's own task(s) exclusively healthy "
        f"(no stray targets left from the previous deployment) within "
        f"{timeout_seconds}s (new task private IPs: {last_new_task_ips or 'not registered yet'}; "
        f"all registered targets: {last_states or 'none'}) — check the container's real "
        f"health_check_path and that it's actually listening on the declared port before retrying."
    )
