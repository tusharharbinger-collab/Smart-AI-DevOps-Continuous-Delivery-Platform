"""
services/pipeline-worker/src/aws/ecs_onboarding.py

Module 8 — real AWS ECS Fargate onboarding, the second genuine deployment
target alongside Kubernetes/Kind. Every function here makes a real AWS API
call (no mocking, no simulation) and is idempotent — safe to call twice for
the same service, matching `k8s/onboarding.py`'s own idempotency
discipline, since a retried onboarding call (or a rebuilt pipeline-worker
container) must never fail just because the resources already exist.

Cost-conscious by construction: ONE shared ECS cluster (free — only the
tasks running inside cost anything), ONE shared Application Load Balancer
across every project (routing via listener rules, the ALB equivalent of
HTTPRoute's PathPrefix match — not a new ~$16/month ALB per project), and
tasks placed directly in the default VPC's public subnets with a public IP
(no NAT Gateway, which would otherwise add ~$32/month for zero benefit
here). Real production security still applies: task security groups only
accept inbound traffic from the ALB's security group, never straight from
the internet.
"""
import time

import boto3
import structlog

from shared.aws_ecs_actuation import (
    register_task_definition,
    ensure_service,
    set_traffic_weights,
    wait_for_service_stable,
)
from src.aws.ecs_manifest import (
    EcsOnboardingSpec,
    SHARED_ALB_NAME,
    SHARED_ALB_SECURITY_GROUP_NAME,
    TASK_EXECUTION_ROLE_NAME,
)

__all__ = [
    "register_task_definition",
    "ensure_service",
    "set_traffic_weights",
    "wait_for_service_stable",
]

logger = structlog.get_logger(__name__)

TASK_EXECUTION_TRUST_POLICY = """{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}"""
TASK_EXECUTION_MANAGED_POLICY_ARN = (
    "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
)


def _clients(region: str) -> dict:
    return {
        "ecs": boto3.client("ecs", region_name=region),
        "ec2": boto3.client("ec2", region_name=region),
        "elbv2": boto3.client("elbv2", region_name=region),
        "iam": boto3.client("iam", region_name=region),
        "logs": boto3.client("logs", region_name=region),
    }


def ensure_cluster(ecs, cluster_name: str) -> str:
    resp = ecs.create_cluster(clusterName=cluster_name)
    logger.info("ecs_cluster_ensured", cluster=cluster_name, status=resp["cluster"]["status"])
    return resp["cluster"]["clusterArn"]


def ensure_task_execution_role(iam) -> str:
    try:
        role = iam.get_role(RoleName=TASK_EXECUTION_ROLE_NAME)
        return role["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        pass
    role = iam.create_role(
        RoleName=TASK_EXECUTION_ROLE_NAME,
        AssumeRolePolicyDocument=TASK_EXECUTION_TRUST_POLICY,
        Description="Shared ECS task execution role for every smartcd-onboarded service (pulls from ECR, writes to CloudWatch Logs).",
    )
    iam.attach_role_policy(RoleName=TASK_EXECUTION_ROLE_NAME, PolicyArn=TASK_EXECUTION_MANAGED_POLICY_ARN)
    logger.info("ecs_task_execution_role_created", role_arn=role["Role"]["Arn"])
    # IAM role creation is eventually consistent — the very next call (task
    # definition registration referencing this role) can otherwise 400 with
    # "role cannot be assumed" for a few seconds on a freshly created role.
    time.sleep(8)
    return role["Role"]["Arn"]


def _default_vpc_public_subnets(ec2) -> tuple[str, list[str]]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError(
            "No default VPC found in this account/region — ECS onboarding needs a VPC with public "
            "subnets; create one or point this at a non-default VPC before retrying."
        )
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "map-public-ip-on-launch", "Values": ["true"]}]
    )["Subnets"]
    subnet_ids = [s["SubnetId"] for s in subnets][:3]
    if len(subnet_ids) < 2:
        raise RuntimeError(f"Default VPC {vpc_id} has fewer than 2 public subnets — ALB requires at least 2.")
    return vpc_id, subnet_ids


def _ensure_security_group(ec2, name: str, vpc_id: str, description: str) -> str:
    existing = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc_id]}]
    )["SecurityGroups"]
    if existing:
        return existing[0]["GroupId"]
    sg = ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc_id)
    logger.info("ecs_security_group_created", name=name, group_id=sg["GroupId"])
    return sg["GroupId"]


def ensure_alb_security_group(ec2, vpc_id: str) -> str:
    sg_id = _ensure_security_group(
        ec2, SHARED_ALB_SECURITY_GROUP_NAME, vpc_id, "Shared smartcd platform ALB - public HTTP ingress"
    )
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
            ],
        )
    except ec2.exceptions.ClientError as e:
        if "InvalidPermission.Duplicate" not in str(e):
            raise
    return sg_id


def ensure_task_security_group(ec2, vpc_id: str, name: str, port: int, alb_sg_id: str) -> str:
    sg_id = _ensure_security_group(ec2, name, vpc_id, f"smartcd task SG for {name} - ALB-only ingress on {port}")
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "UserIdGroupPairs": [{"GroupId": alb_sg_id}]}
            ],
        )
    except ec2.exceptions.ClientError as e:
        if "InvalidPermission.Duplicate" not in str(e):
            raise
    return sg_id


def ensure_shared_alb(elbv2, vpc_id: str, subnet_ids: list[str], alb_sg_id: str) -> dict:
    """Returns {"alb_arn", "dns_name", "listener_arn"}. Idempotent — an ALB
    with this exact name reused across every project, matching the
    Kubernetes side's one-shared-Gateway design."""
    # Real bug found live: unlike describe_target_groups (which errors the
    # same way, already handled below in _target_group_exists),
    # describe_load_balancers raises LoadBalancerNotFoundException rather
    # than returning an empty list for a name that doesn't exist yet — the
    # very first call on a fresh account crashed here instead of proceeding
    # to create it.
    try:
        existing = elbv2.describe_load_balancers(Names=[SHARED_ALB_NAME])["LoadBalancers"]
    except elbv2.exceptions.LoadBalancerNotFoundException:
        existing = []
    if not existing:
        elbv2.create_load_balancer(
            Name=SHARED_ALB_NAME,
            Subnets=subnet_ids,
            SecurityGroups=[alb_sg_id],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
        )
        logger.info("ecs_alb_created", name=SHARED_ALB_NAME)
        # Provisioning an ALB is genuinely asynchronous — poll for "active"
        # rather than assuming the create call means it's ready to attach a
        # listener to.
        for _ in range(30):
            existing = elbv2.describe_load_balancers(Names=[SHARED_ALB_NAME])["LoadBalancers"]
            if existing[0]["State"]["Code"] == "active":
                break
            time.sleep(5)
    alb = existing[0]
    alb_arn = alb["LoadBalancerArn"]

    listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    http_listener = next((l for l in listeners if l["Port"] == 80), None)
    if not http_listener:
        # Default action: a clean, explicit 404 for any path no project has
        # claimed yet — the ALB equivalent of Envoy's own "no route matched"
        # behavior, not a silent connection failure.
        http_listener = elbv2.create_listener(
            LoadBalancerArn=alb_arn,
            Protocol="HTTP",
            Port=80,
            DefaultActions=[
                {
                    "Type": "fixed-response",
                    "FixedResponseConfig": {
                        "StatusCode": "404",
                        "ContentType": "text/plain",
                        "MessageBody": "No service registered at this path.",
                    },
                }
            ],
        )["Listeners"][0]
        logger.info("ecs_alb_listener_created", alb=SHARED_ALB_NAME)

    return {"alb_arn": alb_arn, "dns_name": alb["DNSName"], "listener_arn": http_listener["ListenerArn"]}


def ensure_target_group(elbv2, name: str, vpc_id: str, port: int, health_check_path: str) -> str:
    existing = elbv2.describe_target_groups(Names=[name])["TargetGroups"] if _target_group_exists(elbv2, name) else []
    if existing:
        return existing[0]["TargetGroupArn"]
    tg = elbv2.create_target_group(
        Name=name,
        Protocol="HTTP",
        Port=port,
        VpcId=vpc_id,
        TargetType="ip",
        HealthCheckPath=health_check_path,
        HealthCheckIntervalSeconds=15,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        # Guaranteed Live Web App CI/CD — real gap this closes: this
        # defaulted to boto3's bare Matcher (HTTP 200 only), which marks a
        # target "unhealthy" on any real 3xx redirect (nginx's own default
        # behavior on some setups, a Next.js redirect) even though the app
        # itself is completely healthy — killing/replacing tasks in a loop
        # for something that isn't a real failure. 200-399 is the same
        # success-code range ALB's own console default suggests for a
        # typical web app.
        Matcher={"HttpCode": "200-399"},
    )["TargetGroups"][0]
    logger.info("ecs_target_group_created", name=name, arn=tg["TargetGroupArn"])
    return tg["TargetGroupArn"]


def _target_group_exists(elbv2, name: str) -> bool:
    try:
        elbv2.describe_target_groups(Names=[name])
        return True
    except elbv2.exceptions.TargetGroupNotFoundException:
        return False


def ensure_listener_rule(
    elbv2,
    listener_arn: str,
    path_prefix: str,
    baseline_tg_arn: str,
    canary_tg_arn: str,
    baseline_weight: int,
    canary_weight: int,
    priority: int,
) -> str:
    """
    Real weighted traffic split — the direct ALB equivalent of the
    Kubernetes side's `HTTPRoute.spec.rules[].backendRefs[].weight`. One
    rule per project, matched by PathPrefix (same match type Envoy uses),
    forwarding to both target groups with their relative weights.
    Idempotent: updates the existing rule's actions/weights if this
    project's rule already exists, rather than erroring or duplicating it.
    """
    condition = [{"Field": "path-pattern", "Values": [f"{path_prefix}*"]}]
    action = [
        {
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": baseline_tg_arn, "Weight": baseline_weight},
                    {"TargetGroupArn": canary_tg_arn, "Weight": canary_weight},
                ]
            },
        }
    ]
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    existing_rule = next(
        (
            r
            for r in rules
            if r.get("Conditions")
            and r["Conditions"][0].get("Values") == condition[0]["Values"]
        ),
        None,
    )
    if existing_rule:
        elbv2.modify_rule(RuleArn=existing_rule["RuleArn"], Conditions=condition, Actions=action)
        logger.info("ecs_listener_rule_updated", path_prefix=path_prefix)
        return existing_rule["RuleArn"]

    rule = elbv2.create_rule(
        ListenerArn=listener_arn, Conditions=condition, Priority=priority, Actions=action
    )["Rules"][0]
    logger.info("ecs_listener_rule_created", path_prefix=path_prefix, priority=priority)
    return rule["RuleArn"]


def _next_free_priority(elbv2, listener_arn: str) -> int:
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    used = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
    priority = 1
    while priority in used:
        priority += 1
    return priority


def ensure_log_group(logs, name: str) -> None:
    try:
        logs.create_log_group(logGroupName=name)
        logger.info("ecs_log_group_created", name=name)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass


def onboard_ecs_service(spec: EcsOnboardingSpec, image_tag_baseline: str | None = None, image_tag_canary: str | None = None) -> dict:
    """
    Real gap found live (2026-09-15): the platform could build and push a
    real image to ECR, but had no way to actually RUN it on AWS — this is
    that missing half. Applies every real AWS resource this needs (cluster,
    IAM role, security groups, shared ALB, target groups, listener rule,
    task definitions, services) and returns a real, live-reachable URL.
    Every step is idempotent, matching onboard_service()'s own contract on
    the Kubernetes side — safe to call again for the same project.
    """
    c = _clients(spec.region)
    vpc_id, subnet_ids = _default_vpc_public_subnets(c["ec2"])

    cluster_arn = ensure_cluster(c["ecs"], spec.cluster_name)
    execution_role_arn = ensure_task_execution_role(c["iam"])
    alb_sg_id = ensure_alb_security_group(c["ec2"], vpc_id)
    task_sg_id = ensure_task_security_group(c["ec2"], vpc_id, spec.security_group_name, spec.port, alb_sg_id)
    alb = ensure_shared_alb(c["elbv2"], vpc_id, subnet_ids, alb_sg_id)
    ensure_log_group(c["logs"], spec.log_group_name)

    baseline_tg_arn = ensure_target_group(
        c["elbv2"], spec.baseline_target_group_name, vpc_id, spec.port, spec.health_check_path
    )
    canary_tg_arn = ensure_target_group(
        c["elbv2"], spec.canary_target_group_name, vpc_id, spec.port, spec.health_check_path
    )

    # Real constraint found live: ECS's CreateService rejects a target group
    # with "does not have an associated load balancer" unless that target
    # group is ALREADY referenced by a real listener (rule), so the
    # listener rule has to exist before the services do — the reverse of
    # what seemed like the natural build order (provision compute, then
    # wire routing to it).
    priority = _next_free_priority(c["elbv2"], alb["listener_arn"])
    ensure_listener_rule(
        c["elbv2"], alb["listener_arn"], spec.path_prefix, baseline_tg_arn, canary_tg_arn,
        baseline_weight=100, canary_weight=0, priority=priority,
    )

    baseline_task_def = register_task_definition(
        c["ecs"], spec.task_family_baseline, f"{spec.image}:{image_tag_baseline or spec.baseline_tag}",
        spec.port, spec.cpu, spec.memory, execution_role_arn, spec.log_group_name, spec.region,
        spec.service_name, "baseline", image_tag_baseline or spec.baseline_tag,
        path_prefix=spec.path_prefix,
    )
    canary_task_def = register_task_definition(
        c["ecs"], spec.task_family_canary, f"{spec.image}:{image_tag_canary or spec.canary_tag}",
        spec.port, spec.cpu, spec.memory, execution_role_arn, spec.log_group_name, spec.region,
        spec.service_name, "canary", image_tag_canary or spec.canary_tag,
        path_prefix=spec.path_prefix,
    )

    ensure_service(
        c["ecs"], spec.cluster_name, spec.baseline_service_name, baseline_task_def, baseline_tg_arn,
        spec.service_name, spec.port, subnet_ids, task_sg_id, spec.desired_count,
    )
    ensure_service(
        c["ecs"], spec.cluster_name, spec.canary_service_name, canary_task_def, canary_tg_arn,
        spec.service_name, spec.port, subnet_ids, task_sg_id, spec.desired_count,
    )

    live_url = f"http://{alb['dns_name']}{spec.path_prefix}"
    logger.info("ecs_service_onboarded", service_name=spec.service_name, live_url=live_url)
    return {
        "service_name": spec.service_name,
        "cluster_arn": cluster_arn,
        "alb_dns_name": alb["dns_name"],
        "live_url": live_url,
        "baseline_target_group_arn": baseline_tg_arn,
        "canary_target_group_arn": canary_tg_arn,
    }


def deprovision_ecs_service(region: str, service_name: str, path_prefix: str | None = None, cluster: str = "smartcd-platform") -> dict:
    """
    Real gap found live (2026-09-15): api-gateway's DELETE /projects/{id}
    only ever called pipeline-worker's Kubernetes-side `/services/deprovision`
    — a project onboarded with `deploy_target: "aws_ecs"` had its real ECS
    services, target groups, and ALB listener rule left running (and
    billing) forever. Symmetric to `k8s/onboarding.py::deprovision_service`:
    best-effort and idempotent — every delete ignores "already gone"
    (ServiceNotFoundException / TargetGroupNotFoundException / a
    already-missing listener rule), and one object's absence never blocks
    deleting the rest. Never touches the shared ALB/cluster/listener itself
    (other projects depend on them) — only this project's own rule, target
    groups, and services.
    """
    ecs = boto3.client("ecs", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    baseline_service_name = f"{service_name}-baseline"
    canary_service_name = f"{service_name}-canary"
    baseline_tg_name = f"{service_name}-baseline"[:32]
    canary_tg_name = f"{service_name}-canary"[:32]
    effective_path_prefix = path_prefix or f"/api/v1/{service_name}"
    deleted: dict[str, list[str]] = {"services": [], "target_groups": [], "listener_rules": []}

    for name in (baseline_service_name, canary_service_name):
        try:
            existing = ecs.describe_services(cluster=cluster, services=[name])["services"]
            active = [s for s in existing if s["status"] != "INACTIVE"]
            if active:
                ecs.delete_service(cluster=cluster, service=name, force=True)
                deleted["services"].append(name)
        except ecs.exceptions.ClusterNotFoundException:
            pass

    try:
        albs = elbv2.describe_load_balancers(Names=[SHARED_ALB_NAME])["LoadBalancers"]
    except elbv2.exceptions.LoadBalancerNotFoundException:
        albs = []
    if albs:
        listeners = elbv2.describe_listeners(LoadBalancerArn=albs[0]["LoadBalancerArn"])["Listeners"]
        listener = next((l for l in listeners if l["Port"] == 80), None)
        if listener:
            rules = elbv2.describe_rules(ListenerArn=listener["ListenerArn"])["Rules"]
            rule = next(
                (r for r in rules if r.get("Conditions") and r["Conditions"][0].get("Values") == [f"{effective_path_prefix}*"]),
                None,
            )
            if rule:
                elbv2.delete_rule(RuleArn=rule["RuleArn"])
                deleted["listener_rules"].append(effective_path_prefix)

    # Target groups can't be deleted while a listener rule still references
    # them — the rule delete above must happen first, which is why this
    # loop runs last.
    for name in (baseline_tg_name, canary_tg_name):
        try:
            tg = elbv2.describe_target_groups(Names=[name])["TargetGroups"]
        except elbv2.exceptions.TargetGroupNotFoundException:
            continue
        if tg:
            elbv2.delete_target_group(TargetGroupArn=tg[0]["TargetGroupArn"])
            deleted["target_groups"].append(name)

    logger.info("ecs_service_deprovisioned", service_name=service_name, **deleted)
    return {"status": "deprovisioned", "deleted": deleted}
