"""
shared/component_catalog.py

Phase C of AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2 - the "Add a component" picker's full
resource catalog, plus the compatibility check that runs BEFORE any full infra-proposal regeneration.

Design: a deterministic rule table is tried first (fast, free, no LLM call) for every catalog entry -
these are all well-understood, common AWS resource types, so most compatibility questions have a real,
knowable answer without asking a model. Only a resource type NOT in this catalog at all (a genuinely
free-form request) falls back to the LLM router (see check_component_compatibility below). This keeps the
common path instant and free, and reserves the model for the case that actually needs judgment.

Every catalog entry deliberately excludes anything that would recreate a platform-owned resource (the
shared ALB, the shared ECS cluster itself, listeners/listener-rules/target-groups) - this platform's own
invariant is one shared ALB + one shared ECS cluster for every onboarded project, and a per-project "add a
component" flow must never be able to contradict that. See CLAUDE.md's "invariant" list and
infra_generator.py's `_PLATFORM_OWNED_TYPES`.
"""
from dataclasses import dataclass, field
from typing import Any

from shared.llm_router import AllProvidersFailedError, call_llm


@dataclass(frozen=True)
class ComponentParam:
    name: str
    type: str  # "string" | "integer" | "boolean"
    description: str
    required: bool = False


@dataclass(frozen=True)
class ComponentCatalogEntry:
    resource_type: str
    display_name: str
    category: str
    description: str
    params: list[ComponentParam] = field(default_factory=list)


# Matches AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2's category table exactly - the full catalog,
# not a trimmed example list.
COMPONENT_CATALOG: list[ComponentCatalogEntry] = [
    ComponentCatalogEntry(
        "ec2_instance", "EC2 instance", "Compute",
        "A standalone virtual machine - typically a bastion/admin host, never this project's primary compute.",
        [
            ComponentParam("instance_type", "string", "e.g. t3.micro", required=True),
            ComponentParam("purpose", "string", "What this instance is for (e.g. 'SSH bastion to the database')", required=True),
        ],
    ),
    ComponentCatalogEntry(
        "fargate_service", "Additional Fargate/ECS service", "Compute",
        "A second, project-specific ECS service in the shared cluster (not baseline/canary - those already exist).",
        [ComponentParam("service_name", "string", "A name distinct from this project's baseline/canary services", required=True)],
    ),
    ComponentCatalogEntry(
        "lambda_function", "Lambda function", "Compute",
        "A standalone serverless function, invoked independently of the main service.",
        [ComponentParam("runtime", "string", "e.g. python3.12, nodejs20.x", required=True)],
    ),
    ComponentCatalogEntry(
        "batch_job_queue", "Batch job queue", "Compute",
        "An AWS Batch job queue for scheduled/background compute work.",
        [],
    ),
    ComponentCatalogEntry("s3_bucket", "S3 bucket", "Storage", "An object storage bucket.",
                           [ComponentParam("versioning", "boolean", "Enable object versioning"),
                            ComponentParam("public_access", "boolean", "Allow public read access (default: blocked)")]),
    ComponentCatalogEntry("efs_file_system", "EFS file system", "Storage", "A shared, network-attached file system.", []),
    ComponentCatalogEntry("ebs_volume", "Additional EBS volume", "Storage", "Extra block storage attached to an EC2 instance.",
                           [ComponentParam("size_gb", "integer", "Volume size in GiB", required=True)]),
    ComponentCatalogEntry("rds_read_replica", "RDS read replica", "Database", "A read-only replica of an existing RDS instance.", []),
    ComponentCatalogEntry("dynamodb_table", "DynamoDB table", "Database", "A managed NoSQL table.",
                           [ComponentParam("partition_key", "string", "Partition key attribute name", required=True)]),
    ComponentCatalogEntry("aurora_serverless", "Aurora Serverless", "Database", "An auto-scaling serverless relational database.", []),
    ComponentCatalogEntry("elasticache_node", "Additional ElastiCache node/cluster", "Cache", "Extra cache capacity.", []),
    ComponentCatalogEntry("sqs_queue", "SQS queue", "Messaging", "A managed message queue.",
                           [ComponentParam("fifo", "boolean", "FIFO ordering (default: standard)"),
                            ComponentParam("visibility_timeout_seconds", "integer", "Message visibility timeout")]),
    ComponentCatalogEntry("sns_topic", "SNS topic", "Messaging", "A managed pub/sub topic.", []),
    ComponentCatalogEntry("eventbridge_rule", "EventBridge rule", "Messaging", "A scheduled or event-pattern-triggered rule.", []),
    ComponentCatalogEntry("cloudfront_distribution", "CloudFront distribution", "Networking", "A CDN in front of a public-facing app.", []),
    ComponentCatalogEntry("security_group", "Additional security group", "Networking", "Extra network access control.", []),
    ComponentCatalogEntry("route53_record", "Route 53 record", "Networking", "A DNS record in an existing hosted zone.",
                           [ComponentParam("hosted_zone_domain", "string", "The domain already managed in Route 53", required=True)]),
    ComponentCatalogEntry("secrets_manager_secret", "Secrets Manager secret", "Security/Secrets", "A managed secret.", []),
    ComponentCatalogEntry("kms_key", "KMS key", "Security/Secrets", "A customer-managed encryption key.", []),
    ComponentCatalogEntry("waf_rule", "WAF rule", "Security/Secrets", "A web-application-firewall rule attached to an existing ALB/CloudFront.", []),
    ComponentCatalogEntry("cloudwatch_alarm", "Additional CloudWatch alarm", "Observability", "A custom metric alarm.", []),
    ComponentCatalogEntry("cloudwatch_log_group", "CloudWatch log group", "Observability", "A log group with custom retention.",
                           [ComponentParam("retention_days", "integer", "Log retention in days", required=True)]),
]

_CATALOG_BY_TYPE = {e.resource_type: e for e in COMPONENT_CATALOG}


def catalog_shape_hint(resource_type: str) -> str | None:
    """
    AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 - real gap this closes: infra_generator.py's
    extras-only mode only ever knew how to describe FIVE fixed addition kinds (database/cache/
    object_storage/worker_service/extra_services) - a catalog pick like "ec2_instance" or "sqs_queue" had
    no shape description for the extras-only prompt to use, so it silently produced an empty instruction.
    This gives every catalog entry a real shape hint (its own display name + description) so the FULL
    catalog works for extras-only generation, not just the three basic IntentSpec-toggle-backed kinds.
    """
    entry = _CATALOG_BY_TYPE.get(resource_type)
    return f"{entry.display_name} - {entry.description}" if entry else None


def list_component_catalog() -> list[dict[str, Any]]:
    """The full catalog, grouped implicitly by `category` - the frontend groups by that field."""
    return [
        {
            "resource_type": e.resource_type,
            "display_name": e.display_name,
            "category": e.category,
            "description": e.description,
            "params": [{"name": p.name, "type": p.type, "description": p.description, "required": p.required} for p in e.params],
        }
        for e in COMPONENT_CATALOG
    ]


def _deterministic_check(resource_type: str, params: dict[str, Any], archetype: str, deploy_target: str, intent_spec: dict[str, Any]) -> dict[str, Any] | None:
    """Returns a real compatibility verdict for the well-known rules below, or None if this resource type
    needs the LLM fallback (either not in the catalog, or a nuance this table doesn't cover)."""
    entry = _CATALOG_BY_TYPE.get(resource_type)
    if entry is None:
        return None  # Unknown/custom type - let the LLM fallback reason about it.

    caveats: list[str] = []

    if resource_type == "ec2_instance":
        return {
            "compatible": True,
            "explanation": f"An EC2 instance can be added alongside this project's {deploy_target} compute. It runs independently and is typically used as a bastion/admin host, not as this project's primary application compute.",
            "caveats": ["This instance is NOT part of the baseline/canary rollout - it never receives traffic from the load balancer."],
        }

    if resource_type == "fargate_service":
        if deploy_target != "aws_ecs":
            return {
                "compatible": False,
                "explanation": f"This project deploys to {deploy_target}, not AWS ECS Fargate - an additional Fargate service doesn't apply here.",
                "caveats": ["If you meant an additional Kubernetes Deployment, describe that instead via a free-text prompt."],
            }
        return {
            "compatible": True,
            "explanation": "A new, distinct ECS service can be added in the platform's shared cluster alongside this project's existing baseline/canary services.",
            "caveats": ["This is a genuinely separate service - it does not receive canary traffic and is not part of the rollout gate."],
        }

    if resource_type == "cloudfront_distribution":
        if not intent_spec.get("public_facing", True):
            caveats.append("This project is currently configured as NOT public-facing - a CDN in front of a private app has no public traffic to serve until that changes.")
        return {"compatible": True, "explanation": "A CloudFront distribution can front this project's existing load balancer.", "caveats": caveats}

    if resource_type == "waf_rule":
        return {
            "compatible": True,
            "explanation": "A WAF rule attaches to an existing ALB or CloudFront distribution - this project already has a load balancer via the shared platform ALB.",
            "caveats": ["WAF rules apply at the shared ALB level - review with the platform team before attaching broad rules, since the shared ALB serves other projects too."],
        }

    if resource_type == "route53_record":
        return {
            "compatible": True,
            "explanation": "A DNS record can be added to an existing Route 53 hosted zone.",
            "caveats": [f"Requires a hosted zone for '{params.get('hosted_zone_domain', '<domain>')}' to already exist in this AWS account - this does not create a new hosted zone."],
        }

    if resource_type in ("batch_job_queue",) and archetype not in ("background_worker", "multi_service"):
        caveats.append(f"This project's archetype ('{archetype}') is not a background-worker shape - a batch queue is unusual here but not prohibited.")

    # Default: every other catalog entry is a standard, self-contained AWS resource with no known conflict
    # against any archetype/deploy_target combination in this platform.
    return {
        "compatible": True,
        "explanation": f"{entry.display_name} is a standard, self-contained resource with no conflict against this project's setup.",
        "caveats": caveats,
    }


_COMPATIBILITY_SYSTEM_PROMPT = (
    "You are an AWS infrastructure compatibility checker. Given a requested resource type/parameters and a "
    "project's existing archetype and deploy target, decide whether the resource can genuinely be added "
    "without conflicting with the project's existing infrastructure. Respond ONLY with a valid JSON object: "
    "compatible (boolean), explanation (string, 1-3 sentences, plain language), "
    "caveats (array of strings, may be empty)."
)


async def check_component_compatibility(
    resource_type: str,
    params: dict[str, Any],
    archetype: str,
    deploy_target: str,
    intent_spec: dict[str, Any],
    redis_client=None,
) -> dict[str, Any]:
    """
    The pre-flight check shown to a human BEFORE any full infra-proposal regeneration (see
    projects_router.py's /infra-drafts/{id}/check-component). Tries the deterministic rule table first;
    only a resource type genuinely outside the catalog reaches the LLM router, and if every provider fails
    there, returns an honest "couldn't verify" result rather than fabricating an answer.
    """
    deterministic = _deterministic_check(resource_type, params, archetype, deploy_target, intent_spec)
    if deterministic is not None:
        return {**deterministic, "source": "rule_table"}

    prompt = (
        f"Requested resource type: {resource_type}\n"
        f"Parameters: {params}\n"
        f"Project archetype: {archetype}\n"
        f"Deploy target: {deploy_target}\n"
        f"Relevant intent spec flags: {intent_spec}"
    )
    try:
        result = await call_llm(
            messages=[
                {"role": "system", "content": _COMPATIBILITY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            timeout_seconds=20.0,
            redis_client=redis_client,
        )
        import json

        parsed = json.loads(result["content"])
        return {
            "compatible": bool(parsed.get("compatible", False)),
            "explanation": str(parsed.get("explanation", "")),
            "caveats": [str(c) for c in parsed.get("caveats", [])],
            "source": "llm",
        }
    except (AllProvidersFailedError, Exception) as exc:
        return {
            "compatible": False,
            "explanation": f"Could not verify compatibility for '{resource_type}' - no rule covers it and the AI check failed: {exc}",
            "caveats": ["Try describing this as a free-text edit instead, or try again later."],
            "source": "unavailable",
        }


def build_add_component_instruction(resource_type: str, params: dict[str, Any]) -> str:
    """Turns a structured picker selection into a well-formed free-text instruction string, so the existing
    edit pipeline (generate_infra_proposal(edit=...)) never needs to know whether a request came from the
    picker or the free-text box - one prompt contract, two front doors."""
    entry = _CATALOG_BY_TYPE.get(resource_type)
    display_name = entry.display_name if entry else resource_type
    article = "an" if display_name[:1].upper() in "AEIOU" else "a"
    param_bits = ", ".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))
    if param_bits:
        return f"Add {article} {display_name} with the following configuration: {param_bits}."
    return f"Add {article} {display_name}."
