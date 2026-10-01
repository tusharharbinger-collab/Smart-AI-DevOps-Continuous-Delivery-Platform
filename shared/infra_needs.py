"""
shared/infra_needs.py

Decides WHAT infrastructure a project needs before any AI is asked to design anything.

The platform already creates, for every onboarded project, the shared ALB, the ECS cluster, the target groups, the
baseline + canary Fargate services, their task definitions/security groups and the log group (see
services/pipeline-worker/src/aws/ecs_onboarding.py). An AI-designed stack that recreates those is wrong (a second
ALB costs ~$22/month for nothing) and needlessly complex. So the infra agent is only for what the platform does NOT
provide: a database, a cache, object storage - or a worker service that has no ALB at all.

Pure and deterministic: driven only by the human-locked IntentSpec flags (which are pre-filled from real repo
detection), never by a guess. No third-party imports, so both api-gateway and explainability-service can use it.
"""

PLATFORM_PROVIDES = [
    "Shared Application Load Balancer (created once, reused by every project)",
    "ECS cluster",
    "Baseline and canary Fargate services with their task definitions",
    "Target groups and the weighted listener rule that shifts traffic during a rollout",
    "Container logging (CloudWatch log group)",
]

_DB_ENGINE = {"mysql": "MySQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}

# AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 - real bug found live: a standard-only draft
# (no_additional_infrastructure=true) has no template to EDIT, so a genuine first-ever addition like "add an
# S3 bucket" 409'd with "turn it on in the Requirements above and generate again" - correct advice, but a
# dead end inside a chat interface that's supposed to let exactly this kind of request just work. This is a
# small, bounded, deterministic keyword match against only the THREE addition kinds this platform already
# knows how to design in isolation (database/cache/object_storage - see _ADDITION_SHAPES in
# infra_generator.py) - never a guess beyond a real, recognized keyword, and never covers the full open-ended
# "Add a component" catalog (EC2/Lambda/SQS/etc.), which still needs a real proposal to edit against.
_ADDITION_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("database", ("database", "postgres", "postgresql", "mysql", "rds", "db ", "a db")),
    ("cache", ("cache", "redis", "elasticache", "memcache")),
    ("object_storage", ("s3", "bucket", "object storage", "blob storage", "file storage")),
]


def infer_addition_kind_from_text(text: str) -> str | None:
    """Returns one of "database"/"cache"/"object_storage" if the free-text instruction clearly names it,
    else None (never guesses ambiguous or unrecognized text)."""
    lowered = f" {text.lower()} "
    for kind, keywords in _ADDITION_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return kind
    return None


def analyze_infra_needs(intent_spec: dict, archetype: str, existing_resources: dict | None = None) -> dict:
    """
    Returns {"additions": [{"kind", "reason"}], "needs_ai": bool, "platform_provides": [...], "summary": str}.
    `needs_ai` is False when the platform's standard topology already covers everything the app needs.
    """
    additions: list[dict] = []
    if intent_spec.get("needs_database"):
        engine = _DB_ENGINE.get(str(intent_spec.get("database_type") or "postgres").lower(), "PostgreSQL")
        additions.append({"kind": "database", "reason": f"The app needs a {engine} database, which the platform does not provide."})
    if intent_spec.get("needs_cache"):
        additions.append({"kind": "cache", "reason": "The app needs a cache (Redis), which the platform does not provide."})
    if intent_spec.get("needs_object_storage"):
        additions.append({"kind": "object_storage", "reason": "The app needs object storage (S3), which the platform does not provide."})
    if archetype == "background_worker":
        additions.append({
            "kind": "worker_service",
            "reason": "A background worker has no port or load balancer, so the platform's web-service topology does not fit it.",
        })
    elif archetype == "multi_service":
        additions.append({
            "kind": "extra_services",
            "reason": "This repo runs several services; the platform's standard topology covers only one web service.",
        })

    imports_requested = bool(existing_resources)
    needs_ai = bool(additions) or imports_requested

    if not needs_ai:
        summary = (
            "Nothing extra needs to be built. This app is a self-contained web service - no database, cache or "
            "object storage - so the platform's standard topology already covers it. The shared load balancer, "
            "cluster and the baseline/canary services are created automatically when you create the project."
        )
    elif imports_requested and not additions:
        summary = "You chose to attach existing AWS resources, so only their import is planned - nothing else is created."
    else:
        listed = ", ".join(a["kind"].replace("_", " ") for a in additions)
        summary = (
            f"The platform's standard topology covers the web service; the AI only needs to design the extras: {listed}."
        )
    return {"additions": additions, "needs_ai": needs_ai, "platform_provides": PLATFORM_PROVIDES, "summary": summary}


def build_standard_only_proposal(intent_spec: dict, archetype: str, needs: dict) -> dict:
    """
    The proposal returned when nothing extra is needed. It deliberately has no CloudFormation template: there is
    nothing to provision, so no AI call, no change set and no extra cost. The topology shows the platform's own
    standard shape so the human sees exactly what will exist.
    """
    return {
        "name": f"{archetype}_{intent_spec.get('environment_tier', 'dev')}_standard",
        "no_additional_infrastructure": True,
        "needs_summary": needs["summary"],
        "platform_provides": needs["platform_provides"],
        "additions": [],
        "topology": {
            "nodes": [
                {"id": "alb", "type": "alb", "label": "Shared ALB (platform-provided)"},
                {"id": "baseline", "type": "ecs_service", "label": "Baseline service (platform-provided)"},
                {"id": "canary", "type": "ecs_service", "label": "Canary service (platform-provided)"},
            ],
            "edges": [{"source": "alb", "target": "baseline"}, {"source": "alb", "target": "canary"}],
        },
        "iac_terraform": "",
        "cloudformation_template": "",
        "estimated_monthly_cost_usd": 0.0,
        "cost_breakdown": [],
        "cost_estimate": {
            "source": "none", "currency": "USD", "errors": 0, "unpriced": [],
            "reason": "No additional infrastructure is provisioned.",
        },
        "policy_checks": [],
        "policy_evaluation": {
            "engine": "none", "policy": None, "allowed": True, "deny": [], "warn": [],
            "reason": "Nothing is provisioned, so there is nothing to check.",
        },
    }
