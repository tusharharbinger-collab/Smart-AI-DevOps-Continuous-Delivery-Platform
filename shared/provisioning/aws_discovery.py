"""
shared/provisioning/aws_discovery.py

AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phase B — read-only enumeration of
AWS resources that already exist, so a human can pick which ones to attach
("existing") instead of having the Infra Architect Agent create duplicates.

Strictly read-only (describe/list calls only) and scoped to the resource
TYPES the chosen archetype actually needs — never "everything in the
account". Nothing here is ever adopted automatically: the human picks from
this list, and `describe_selected` re-verifies each pick against AWS itself
(the client is never trusted for a resource's real configuration — the
agent must describe an imported resource as it actually is, plan §4).
"""
import structlog

from shared.provisioning.aws_session import client as aws_client

logger = structlog.get_logger(__name__)

SLOT_DATABASE = "database"
SLOT_CACHE = "cache"
SLOT_ECS_CLUSTER = "ecs_cluster"
SLOT_LOAD_BALANCER = "load_balancer"

# CloudFormation resource type each slot maps to. Kept here as the single
# source of truth for pipeline-worker/api-gateway; explainability-service
# mirrors it by value (it has no dependency on shared/).
SLOT_CFN_TYPES = {
    SLOT_DATABASE: "AWS::RDS::DBInstance",
    SLOT_CACHE: "AWS::ElastiCache::CacheCluster",
    SLOT_ECS_CLUSTER: "AWS::ECS::Cluster",
    SLOT_LOAD_BALANCER: "AWS::ElasticLoadBalancingV2::LoadBalancer",
}

ARCHETYPE_SLOTS: dict[str, list[str]] = {
    "static_site": [SLOT_LOAD_BALANCER],
    "stateless_web_service": [SLOT_ECS_CLUSTER, SLOT_LOAD_BALANCER],
    "web_service_with_database": [SLOT_ECS_CLUSTER, SLOT_LOAD_BALANCER, SLOT_DATABASE],
    "web_service_with_database_and_cache": [SLOT_ECS_CLUSTER, SLOT_LOAD_BALANCER, SLOT_DATABASE, SLOT_CACHE],
    "background_worker": [SLOT_ECS_CLUSTER, SLOT_DATABASE, SLOT_CACHE],
    "multi_service": [SLOT_ECS_CLUSTER, SLOT_LOAD_BALANCER, SLOT_DATABASE, SLOT_CACHE],
}


def slots_for_archetype(archetype: str) -> list[str]:
    return ARCHETYPE_SLOTS.get(archetype, [SLOT_ECS_CLUSTER, SLOT_LOAD_BALANCER])


def _list_databases(region: str, connection: dict | None = None) -> list[dict]:
    rds = aws_client("rds", region, connection)
    return [
        {
            "id": d["DBInstanceIdentifier"],
            "label": d["DBInstanceIdentifier"],
            "details": {
                "engine": d.get("Engine"), "engine_version": d.get("EngineVersion"),
                "instance_class": d.get("DBInstanceClass"), "multi_az": d.get("MultiAZ"),
                "allocated_storage_gb": d.get("AllocatedStorage"), "status": d.get("DBInstanceStatus"),
                "storage_encrypted": d.get("StorageEncrypted"),
            },
        }
        for d in rds.describe_db_instances()["DBInstances"]
    ]


def _list_caches(region: str, connection: dict | None = None) -> list[dict]:
    ec = aws_client("elasticache", region, connection)
    return [
        {
            "id": c["CacheClusterId"],
            "label": c["CacheClusterId"],
            "details": {
                "engine": c.get("Engine"), "engine_version": c.get("EngineVersion"),
                "node_type": c.get("CacheNodeType"), "num_nodes": c.get("NumCacheNodes"),
                "status": c.get("CacheClusterStatus"),
            },
        }
        for c in ec.describe_cache_clusters()["CacheClusters"]
    ]


def _list_ecs_clusters(region: str, connection: dict | None = None) -> list[dict]:
    ecs = aws_client("ecs", region, connection)
    arns = ecs.list_clusters()["clusterArns"]
    if not arns:
        return []
    return [
        {
            "id": c["clusterName"],
            "label": c["clusterName"],
            "details": {
                "status": c.get("status"), "running_tasks": c.get("runningTasksCount"),
                "active_services": c.get("activeServicesCount"),
            },
        }
        for c in ecs.describe_clusters(clusters=arns)["clusters"]
    ]


def _list_load_balancers(region: str, connection: dict | None = None) -> list[dict]:
    elbv2 = aws_client("elbv2", region, connection)
    return [
        {
            "id": lb["LoadBalancerName"],
            "label": lb["LoadBalancerName"],
            "details": {
                "type": lb.get("Type"), "scheme": lb.get("Scheme"), "dns_name": lb.get("DNSName"),
                "vpc_id": lb.get("VpcId"), "arn": lb.get("LoadBalancerArn"),
            },
        }
        for lb in elbv2.describe_load_balancers()["LoadBalancers"]
    ]


_LISTERS = {
    SLOT_DATABASE: _list_databases,
    SLOT_CACHE: _list_caches,
    SLOT_ECS_CLUSTER: _list_ecs_clusters,
    SLOT_LOAD_BALANCER: _list_load_balancers,
}


def discover_existing(archetype: str, region: str, connection: dict | None = None) -> dict[str, list[dict]]:
    """slot -> candidate list, only for the slots this archetype needs. A slot whose AWS call
    fails returns an empty list plus is logged — one unreadable service must not hide the rest."""
    result: dict[str, list[dict]] = {}
    for slot in slots_for_archetype(archetype):
        try:
            result[slot] = _LISTERS[slot](region, connection)
        except Exception as e:
            logger.warning("aws_discovery_slot_failed", slot=slot, region=region, error=str(e))
            result[slot] = []
    return result


def describe_selected(selection: dict[str, str], archetype: str, region: str, connection: dict | None = None) -> dict[str, dict]:
    """
    Re-verifies a human's picks against AWS: {slot: identifier} -> {slot: {"id", "details"}}.
    Raises ValueError for a slot this archetype doesn't use, or an identifier AWS doesn't list.
    """
    allowed = set(slots_for_archetype(archetype))
    verified: dict[str, dict] = {}
    for slot, identifier in selection.items():
        if slot not in allowed:
            raise ValueError(f"Slot '{slot}' is not valid for archetype '{archetype}'.")
        candidates = _LISTERS[slot](region, connection)
        match = next((c for c in candidates if c["id"] == identifier), None)
        if match is None:
            raise ValueError(f"No existing {slot} named '{identifier}' found in {region}.")
        verified[slot] = {"id": match["id"], "details": match["details"]}
    return verified


def get_platform_network_context(region: str, connection: dict | None = None) -> dict:
    """
    The network the platform deploys services into (the default VPC and its subnets - the same choice
    ecs_onboarding makes), so an AI-designed extra such as a database can be placed in it with REAL ids rather
    than invented ones. Read-only. Raises RuntimeError when there is no default VPC (the platform cannot deploy
    there either).
    """
    ec2 = aws_client("ec2", region, connection)
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("No default VPC in this account/region - the platform deploys into the default VPC.")
    vpc = vpcs[0]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["Subnets"]
    # One subnet per availability zone, stable order: an RDS/ElastiCache subnet group needs >= 2 AZs.
    by_az: dict[str, str] = {}
    for s in sorted(subnets, key=lambda x: x["SubnetId"]):
        by_az.setdefault(s["AvailabilityZone"], s["SubnetId"])
    if len(by_az) < 2:
        raise RuntimeError(f"Default VPC {vpc['VpcId']} spans fewer than 2 availability zones.")
    return {"vpc_id": vpc["VpcId"], "vpc_cidr": vpc["CidrBlock"], "subnet_ids": list(by_az.values())[:3], "region": region}
