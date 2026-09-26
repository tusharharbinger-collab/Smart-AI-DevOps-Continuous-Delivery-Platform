"""
services/explainability-service/src/infra_generator.py

Phase 4 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5) — the Infra Architect Agent.
Mirrors pipeline_generator.py's shape almost exactly (Groq, hard timeout,
pydantic-validated result, raises rather than fakes on failure since no
safety action rides on this call) with one upgrade: native structured-
output mode (`response_format: json_schema`, §R.6) instead of plain
`json_object` mode — this is a materially more complex object (topology +
Terraform + cost breakdown + policy checks) than a single YAML string, and
research showed JSON-mode-and-hope breaks schema conformance ~20% of the
time versus <1% for a real enforced JSON Schema.

Archetype-bounded by construction (Ground Rule 0a / §2.3): the model is
never asked to invent a topology. It receives the ALREADY-MATCHED
golden-path archetype (from shared/repo_scanner.py's
match_golden_path_archetype, carried on the locked IntentSpec) and the
system instruction explicitly restricts it to parameterizing that one
archetype's known resource shape — sizing, whether Multi-AZ, region,
naming — never choosing a different architecture.
"""
import asyncio
import json
import os
import re

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Completion ceilings, tunable per Groq plan tier. Measured live: a slimmed edit request is
# ~2,500 prompt + ~3,800 completion tokens and is accepted with the full 8192 ceiling; lowering
# the edit ceiling to 5500 (an earlier, mistaken attempt at avoiding a 413) instead TRUNCATED
# larger edits ("json_validate_failed"). The 413 was fixed by slimming the prompt, not by this.
INFRA_MAX_TOKENS_CREATE = int(os.environ.get("GROQ_INFRA_MAX_TOKENS_CREATE", "8192"))
INFRA_MAX_TOKENS_EDIT = int(os.environ.get("GROQ_INFRA_MAX_TOKENS_EDIT", "8192"))

# Mirrors shared/repo_scanner.py's ARCHETYPE_* constants and
# shared/intent_spec.py's FARGATE_TIER_DEFAULTS by value — this service has
# no dependency on those modules' exact Python objects, only their string
# contract, so the resource list here is deliberately spelled out rather
# than imported.
_ARCHETYPE_RESOURCE_SHAPES: dict[str, str] = {
    "static_site": "An S3 bucket for build output plus a CloudFront distribution (or, to match this "
    "platform's existing shared-ALB model, an nginx container behind the shared ALB). No database, "
    "no cache, no compute autoscaling beyond the container itself.",
    "stateless_web_service": "One ECS Fargate service behind the shared ALB's target group. No "
    "database, no cache. Autoscaling is instance-count only within the given min/max.",
    "web_service_with_database": "One ECS Fargate service behind the shared ALB's target group, plus "
    "one RDS instance (Postgres or MySQL per the spec) sized for the given environment tier.",
    "web_service_with_database_and_cache": "One ECS Fargate service behind the shared ALB's target "
    "group, one RDS instance, and one ElastiCache (Redis) cluster.",
    "background_worker": "One ECS Fargate service with NO ALB target group (no exposed port) — a "
    "worker process only. May still have an RDS/ElastiCache dependency per the spec's needs_database/"
    "needs_cache flags.",
    "multi_service": "Multiple ECS Fargate services, each with its own target group behind the shared "
    "ALB, one per detected service. Shared RDS/ElastiCache only if the spec's needs_database/"
    "needs_cache flags are set.",
}

_SYSTEM_INSTRUCTION_TEMPLATE = (
    "You are an infrastructure design assistant for an enterprise continuous-delivery platform "
    "that provisions onto AWS ECS Fargate behind one shared Application Load Balancer per tenant. "
    "You are given a locked IntentSpec (the human's declared requirements) and the ALREADY-DECIDED "
    "golden-path archetype for this project. You must parameterize that exact archetype — never "
    "propose a different one, never add resources the archetype's shape does not call for. "
    "The archetype '{archetype}' means: {resource_shape} "
    "Respond with a single JSON object matching the given schema exactly: a topology (nodes with "
    "id/type/label, and edges connecting them by node id), Terraform HCL text for the described "
    "resources only (a human-readable audit artifact ONLY — this platform never runs Terraform), "
    "a real AWS CloudFormation template (as a JSON string, escaped correctly for the outer JSON "
    "response) describing the EXACT SAME resources as the topology — this is the artifact that "
    "actually gets deployed via a CloudFormation Change Set, so it must be syntactically valid "
    "CloudFormation JSON with a Resources section covering every node in the topology, an "
    "estimated total monthly USD cost, an itemized cost_breakdown (one entry per priced resource), "
    "and policy_checks (one entry per compliance rule you evaluated against the spec, e.g. "
    "encryption at rest, no public database access, tagged resources — each with whether it "
    "passed and a one-sentence reason). Keep both the Terraform and CloudFormation text minimal "
    "but complete — only the resources this archetype needs, no comments, no extra whitespace — "
    "you have a finite output budget shared across both artifacts plus the rest of this schema. "
    "PLATFORM SECURITY REQUIREMENTS (an independent policy engine checks the CloudFormation template against these, and a "
    "violation blocks the proposal): every AWS::RDS::DBInstance must set StorageEncrypted true and PubliclyAccessible false, "
    "and must NEVER contain a literal MasterUserPassword - set ManageMasterUserPassword true (with a MasterUsername) instead; "
    "no security group may allow ingress from 0.0.0.0/0 or ::/0 except TCP 80 and 443; no IAM statement may allow Action '*' "
    "on Resource '*'; every AWS::S3::Bucket must set PublicAccessBlockConfiguration with all four Block/Ignore/Restrict "
    "flags true and must not use a public AccessControl; use an internal load balancer scheme when the IntentSpec says "
    "public_facing is false; for production tier set MultiAZ true on databases."
)


_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
# A hint longer than this is a QUOTA reset (e.g. the daily token cap: "try again in 56m57s"), not a
# per-minute window - no bounded in-request wait can clear it, so waiting only delays the failure.
_GIVE_UP_IF_WAIT_EXCEEDS_SECONDS = 60.0


def _parse_retry_after_seconds(groq_error_body: str, default: float = 15.0) -> float:
    """
    Groq's 429 body reads like `"...Please try again in 14.94s..."`, `"...in 750ms..."` or, for the daily
    cap, `"...in 56m57.552s..."` (found live: the old seconds-only pattern read that as 57 seconds and burned
    three pointless 15s waits). Sums every h/m/s/ms component; falls back to `default` if the shape changes.
    """
    match = re.search(r"try again in ((?:[\d.]+(?:ms|h|m|s))+)", groq_error_body)
    if not match:
        return default
    return sum(float(n) * _DURATION_UNITS[u] for n, u in re.findall(r"([\d.]+)(ms|h|m|s)", match.group(1)))


def _describe_wait(seconds: float) -> str:
    return f"{round(seconds / 60)}m" if seconds >= 90 else f"{round(seconds)}s"


class InfraGenerationError(Exception):
    """Raised when Groq is unavailable or returns an unusable response — no safety action
    depends on this call (Ground Rule 0a: AI proposes, a human approves, OPA/cost logic
    validates before that), so callers should surface a clear error rather than fabricate
    an infra proposal nobody reviewed."""


class _RateLimitExhausted(InfraGenerationError):
    """Groq stayed rate-limited through every bounded wait. Never retried by the correction loop -
    a second full round of waiting would blow api-gateway's 70s timeout for no benefit."""


_MAX_RATE_LIMIT_WAITS = 3
_MAX_RATE_LIMIT_SLEEP_SECONDS = 15.0


def _repair_or_reject_cloudformation_json(raw: str) -> str:
    """
    Real bug found live: Groq's strict json_schema mode guarantees the
    OUTER response is valid JSON and that `cloudformation_template` is a
    string — it cannot guarantee the STRING'S OWN CONTENT is well-formed
    CloudFormation JSON, since that's a semantic property, not a structural
    one the schema can enforce. Observed failure mode: the model double-
    escaped its own output (e.g. produced the two literal characters `\"`
    where a plain `"` belonged), which parses fine as an outer JSON string
    but is not valid JSON once you look at what that string actually
    contains — and would reach AWS's real create_change_set as a malformed
    template, failing with an opaque CloudFormation ValidationError instead
    of a clear error here. One repair attempt (undo exactly one level of
    over-escaping) before giving up loudly — never silently pass through
    content nothing has confirmed is real, valid CloudFormation.
    """
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Undo string-escaping one level at a time, up to 3 levels: the model has been seen to
    # over-escape more than once (found live on edits, where the template is shown to it as an
    # object). Decoding as a JSON string is the principled inverse of escaping - it handles \",
    # \\, \n and \uXXXX correctly, unlike a blind find-and-replace (kept only as a fallback).
    candidate = raw
    last_error: Exception | None = None
    for _ in range(4):
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError as e:
            last_error = e
            salvaged = _drop_stray_closers(candidate)
            if salvaged is not None:
                return salvaged
        try:
            candidate = json.loads('"' + candidate + '"')
        except json.JSONDecodeError:
            candidate = candidate.replace('\\"', '"').replace("\\\\", "\\")
    tail = ""
    if isinstance(last_error, json.JSONDecodeError) and "Extra data" in str(last_error):
        tail = f" (trailing text: {raw[last_error.pos:last_error.pos + 60]!r})"
    raise InfraGenerationError(
        f"AI generated a cloudformation_template that is not valid JSON even after a repair "
        f"attempt — refusing to pass a malformed template forward: {last_error}{tail}"
    )


_STRAY_CLOSERS = set("}] ,\n\r\t")


def _drop_stray_closers(text: str) -> str | None:
    """
    Found live (intermittent, Groq): a complete, valid CloudFormation object followed by a few extra closing
    brackets ("Extra data"). Accept the leading object ONLY when everything after it is closers/commas/whitespace
    - text that could be dropped content (a quote, a colon, a letter) means resources may have been cut off, so
    that is still rejected. Returns the re-serialized object, or None when it does not apply.
    """
    stripped = text.lstrip()
    try:
        obj, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    rest = stripped[end:]
    if not isinstance(obj, dict) or "Resources" not in obj or (rest and not set(rest) <= _STRAY_CLOSERS):
        return None
    logger.warning("infra_template_stray_closers_dropped", dropped=repr(rest[:40]))
    return json.dumps(obj)


# Mirrors shared/provisioning/aws_discovery.py's SLOT_CFN_TYPES by value (this service has no
# dependency on shared/): which CloudFormation resource type each "existing resource" slot is.
_SLOT_CFN_TYPES = {
    "database": "AWS::RDS::DBInstance",
    "cache": "AWS::ElastiCache::CacheCluster",
    "ecs_cluster": "AWS::ECS::Cluster",
    "load_balancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
}


def _slim_proposal_for_edit(proposal: dict) -> dict:
    """
    Found live: a 9-resource proposal sent whole (Terraform + the CloudFormation template as an
    escaped string + cost/policy lists) made one edit request 8,236 tokens - Groq answers 413
    (not a retryable 429) because a single request exceeds this plan's 8,000 tokens/minute cap.
    The template is the authoritative artifact: send it as a parsed object (escaping roughly
    doubles its token count) plus the topology, and drop the derived, audit-only parts - the
    model regenerates those consistently from the edited template.
    """
    template = proposal.get("cloudformation_template")
    try:
        template = json.loads(template) if isinstance(template, str) else template
    except json.JSONDecodeError:
        pass
    return {
        "name": proposal.get("name"),
        "topology": proposal.get("topology"),
        "cloudformation_template": template,
        "note": "iac_terraform, cost_breakdown and policy_checks omitted - regenerate them to match the edited template.",
    }


def _template_resources(template_json: str) -> dict:
    return json.loads(template_json).get("Resources", {}) or {}


def _require_retain_on_imports(template_json: str, existing_resources: dict) -> None:
    """
    AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md 1.2 - the one safety property that must never depend
    on the model getting it right unprompted: every resource of a type the human attached as
    "existing" must be DeletionPolicy: Retain (CloudFormation also enforces this for imports, but
    failing here gives the model a corrective retry instead of an opaque AWS ValidationError),
    and at least one such resource must exist per attached slot.
    """
    resources = _template_resources(template_json)
    for slot in existing_resources:
        cfn_type = _SLOT_CFN_TYPES.get(slot)
        if cfn_type is None:
            continue
        of_type = {lid: r for lid, r in resources.items() if r.get("Type") == cfn_type}
        if not of_type:
            raise InfraGenerationError(
                f"The human attached an existing {slot} ('{existing_resources[slot]['id']}') but the template has no {cfn_type} resource for it."
            )
        for logical_id, resource in of_type.items():
            if resource.get("DeletionPolicy") != "Retain":
                raise InfraGenerationError(
                    f"{logical_id} ({cfn_type}) represents an imported existing resource and must declare DeletionPolicy: Retain."
                )


def _require_retained_resources_preserved(old_template_json: str, new_template_json: str) -> None:
    """
    2.2 - an edit may add resources or modify a NON-retained one, but must never remove, rename
    or un-retain a DeletionPolicy: Retain resource: that would silently turn "import my existing
    database" into "delete and recreate my existing database".
    """
    old_retained = {lid for lid, r in _template_resources(old_template_json).items() if r.get("DeletionPolicy") == "Retain"}
    new_resources = _template_resources(new_template_json)
    for logical_id in sorted(old_retained):
        resource = new_resources.get(logical_id)
        if resource is None:
            raise InfraGenerationError(f"The edit removed {logical_id}, a Retain (imported/existing) resource - this is never allowed.")
        if resource.get("DeletionPolicy") != "Retain":
            raise InfraGenerationError(f"The edit dropped DeletionPolicy: Retain from {logical_id} - this is never allowed.")


class TopologyNode(BaseModel):
    id: str
    type: str
    label: str


class TopologyEdge(BaseModel):
    source: str
    target: str


class InfraTopology(BaseModel):
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]


class CostBreakdownItem(BaseModel):
    resource: str
    monthly_usd: float


class PolicyCheckResult(BaseModel):
    check: str
    passed: bool
    detail: str


class InfraGenerationResult(BaseModel):
    name: str
    topology: InfraTopology
    iac_terraform: str
    # Phase 7a (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — the artifact that
    # actually gets executed, via a CloudFormation Change Set (never applied
    # directly — always previewed, then a human explicitly executes it).
    # iac_terraform stays a read-only audit artifact; this is the real one.
    cloudformation_template: str
    estimated_monthly_cost_usd: float
    cost_breakdown: list[CostBreakdownItem]
    policy_checks: list[PolicyCheckResult]


def _build_json_schema_response_format() -> dict:
    """
    Native structured-output mode (§R.6) — Groq's OpenAI-compatible
    `response_format: {"type": "json_schema", ...}` enforces the schema at
    the token-generation level rather than hoping a plain "respond with
    JSON" instruction is followed, which research showed drops schema
    conformance from >99% to ~80% on an object this shape (nested
    lists of objects) versus a flat one.
    """
    schema = InfraGenerationResult.model_json_schema()
    _require_no_additional_properties(schema)
    for definition in schema.get("$defs", {}).values():
        _require_no_additional_properties(definition)
    return {
        "type": "json_schema",
        "json_schema": {"name": "infra_generation_result", "schema": schema, "strict": True},
    }


def _require_no_additional_properties(object_schema: dict) -> None:
    """
    Real bug found live (testing this endpoint against the actual Groq
    API, not a mock): pydantic's model_json_schema() never sets
    "additionalProperties": false — Groq/OpenAI's strict json_schema mode
    requires it on EVERY object in the schema (the root AND every nested
    $defs entry) and returns a plain 400 with no field-level detail in the
    error body when it's missing anywhere. Mutates in place.
    """
    if object_schema.get("type") == "object" or "properties" in object_schema:
        object_schema["additionalProperties"] = False


async def generate_infra_proposal(
    intent_spec: dict,
    archetype: str,
    timeout_seconds: float = 60.0,
    existing_resources: dict | None = None,
    edit: dict | None = None,
) -> dict:
    """
    `intent_spec`: the locked, tier-defaulted IntentSpec (see
    shared/intent_spec.py), as a plain dict — this service has no
    dependency on the pydantic model itself, only its serialized shape.
    `archetype`: one of shared/repo_scanner.py's ARCHETYPE_* string values;
    required (never inferred here — that already happened in Phase 1).
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise InfraGenerationError("GROQ_API_KEY is not configured — AI infra generation is unavailable.")

    resource_shape = _ARCHETYPE_RESOURCE_SHAPES.get(
        archetype, "An unrecognized archetype — treat conservatively: propose only the minimum ECS Fargate "
        "service the spec's own needs_database/needs_cache/needs_object_storage flags call for."
    )
    system_instruction = _SYSTEM_INSTRUCTION_TEMPLATE.format(archetype=archetype, resource_shape=resource_shape)
    base_user_content = f"IntentSpec:\n{json.dumps(intent_spec, default=str)}"
    if existing_resources:
        # Verified server-side against AWS (describe_selected), so `details` is ground truth -
        # the template must describe each resource as it ACTUALLY is (plan 4), not as the
        # archetype would ideally want it, or CloudFormation's import will reject the mismatch.
        system_instruction += (
            " The following AWS resources ALREADY EXIST and must be imported, never recreated: "
            f"{json.dumps(existing_resources, default=str)}. For each, emit the matching CloudFormation "
            "resource with DeletionPolicy set to Retain, its real physical identifier (e.g. DBInstanceIdentifier, "
            "ClusterName, Name) exactly as given, and properties that match the given real details. "
            "Only design NEW resources for parts of the archetype not covered by an existing resource."
        )
    if edit:
        system_instruction += (
            " You are EDITING an existing infrastructure proposal, not designing from scratch. The rule above that "
            "restricts you to the archetype's default resource shape applies ONLY when designing from scratch: an "
            "explicit human edit instruction MAY add, remove or change resources beyond that shape (the human reviews "
            "and approves the result), and you MUST actually apply it - returning the proposal unchanged is a failure. "
            "Apply ONLY the human's instruction; preserve every other resource, topology edge, and cost line exactly. NEVER remove, "
            "rename, or replace any resource that declares DeletionPolicy: Retain (an imported, pre-existing "
            "resource) and never drop its DeletionPolicy: Retain - only add resources or modify non-retained ones."
        )
        base_user_content += (
            f"\n\nCurrent proposal:\n{json.dumps(_slim_proposal_for_edit(edit['current_proposal']), default=str)}"
            f"\n\nHuman instruction: {edit['instruction']}"
        )

    # Two attempts total, matching pipeline_generator.py's own established
    # retry contract for this codebase: real LLM output occasionally fails
    # in a way a corrective retry can fix (a malformed cloudformation_template
    # string, or a validation error) — never a third silent attempt.
    last_error: Exception | None = None
    for attempt in range(2):
        user_content = base_user_content
        if last_error is not None:
            user_content += (
                f"\n\nYour previous attempt was rejected for this exact reason — fix it: {last_error}"
            )
        try:
            request_json = {
                "model": GROQ_MODEL,
                "temperature": 0.0,
                # Real bug found live: this object requires the model to generate TWO full IaC
                # artifacts (Terraform text AND a CloudFormation JSON template) plus topology/
                # cost/policy data - without an explicit ceiling, generation was getting cut off
                # mid-JSON, which Groq's strict json_schema mode reports as a plain 400
                # ("json_validate_failed") rather than a truncation error. 8192 is generous
                # headroom for openai/gpt-oss-120b, not a tuned minimum.
                "max_tokens": INFRA_MAX_TOKENS_EDIT if edit else INFRA_MAX_TOKENS_CREATE,
                "response_format": _build_json_schema_response_format(),
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content},
                ],
            }
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                # Real bugs found live (2026-09-25/27): this Groq key's plan caps at 8000 tokens/
                # MINUTE, and one call already uses ~5000 (openai/gpt-oss-120b is a REASONING model
                # - ~2000+ tokens of hidden reasoning before the JSON answer). An EDIT is worse: it
                # also sends the whole current proposal (~5900 tokens requested), so it cannot fit
                # in the rolling window right after a create. A 429 used to fail immediately with no
                # detail; then a single backoff still wasn't enough. Now: wait for the window Groq
                # itself reports (capped), up to _MAX_RATE_LIMIT_WAITS times, INDEPENDENT of the
                # 2-attempt correction-retry budget (a rate limit says nothing about the answer's
                # quality). Total worst case stays under api-gateway's 70s timeout.
                for rate_limit_wait in range(_MAX_RATE_LIMIT_WAITS + 1):
                    response = await client.post(
                        GROQ_API_URL, headers={"Authorization": f"Bearer {api_key}"}, json=request_json
                    )
                    if response.status_code != 429:
                        break
                    hinted_wait = _parse_retry_after_seconds(response.text)
                    if hinted_wait > _GIVE_UP_IF_WAIT_EXCEEDS_SECONDS:
                        raise _RateLimitExhausted(
                            f"Groq rate limit exceeded after retry - the quota resets in about {_describe_wait(hinted_wait)}, "
                            f"too long to wait inside a request: {response.text[:300]}"
                        )
                    retry_after = min(hinted_wait, _MAX_RATE_LIMIT_SLEEP_SECONDS)
                    logger.warning(
                        "groq_infra_generation_rate_limited", retry_after=retry_after,
                        wait=rate_limit_wait, body=response.text[:500],
                    )
                    if rate_limit_wait == _MAX_RATE_LIMIT_WAITS:
                        raise _RateLimitExhausted(f"Groq rate limit exceeded after retry: {response.text[:500]}")
                    await asyncio.sleep(retry_after)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]

            parsed = InfraGenerationResult.model_validate_json(content)
            parsed.cloudformation_template = _repair_or_reject_cloudformation_json(parsed.cloudformation_template)
            if existing_resources:
                _require_retain_on_imports(parsed.cloudformation_template, existing_resources)
            if edit:
                current = edit["current_proposal"]
                if (
                    parsed.cloudformation_template == current.get("cloudformation_template")
                    and parsed.model_dump()["topology"] == current.get("topology")
                ):
                    # Found live: the model returned the proposal byte-identical to an "add a cache"
                    # instruction. A silent no-op must fail loudly and feed back into the corrective retry.
                    raise InfraGenerationError(
                        "The edit produced no change at all - the proposal is identical to the current one. "
                        "Actually apply the human's instruction."
                    )
            if edit and edit["current_proposal"].get("cloudformation_template"):
                _require_retained_resources_preserved(
                    edit["current_proposal"]["cloudformation_template"], parsed.cloudformation_template
                )
            if attempt > 0:
                logger.info("groq_infra_generation_succeeded_on_retry")
            return parsed.model_dump()
        except httpx.TimeoutException as e:
            logger.warning("groq_infra_generation_timeout", timeout_seconds=timeout_seconds)
            raise InfraGenerationError(f"AI infra generation timed out after {timeout_seconds}s.") from e
        except _RateLimitExhausted:
            raise
        except (InfraGenerationError, ValueError) as e:
            # Only retry the classes of failure a corrective prompt can
            # plausibly fix (malformed cloudformation_template, a pydantic
            # ValidationError, or the just-backed-off rate-limit case
            # above) — a timeout or missing-API-key case above already
            # returns/raises immediately instead of reaching here.
            last_error = e
            continue
        except httpx.HTTPStatusError as e:
            # Real bug found live: this used to fall into the generic
            # `except Exception` branch below, which only ever captured
            # httpx's own generic "Client error '400 Bad Request' for url
            # ...' " message — Groq's ACTUAL reason, in the response body,
            # was silently discarded. Every real failure was undiagnosable
            # without reproducing it live against the real API. Surface it.
            body = e.response.text[:1000]
            logger.error("groq_infra_generation_http_error", status_code=e.response.status_code, body=body)
            if attempt == 0 and e.response.status_code == 400 and "json_validate_failed" in body:
                # Groq's strict json_schema mode couldn't finish a valid object (typically a truncated
                # generation) - model-side and often cleared by a second attempt, unlike a real 4xx.
                last_error = InfraGenerationError("The previous attempt did not produce complete, valid JSON - be more concise.")
                continue
            raise InfraGenerationError(f"Groq rejected the request ({e.response.status_code}): {body}") from e
        except Exception as e:
            logger.error("groq_infra_generation_failed", error=str(e))
            raise InfraGenerationError(f"AI infra generation failed: {e}") from e

    logger.warning("groq_infra_generation_failed_after_retry", error=str(last_error))
    raise InfraGenerationError(f"AI infra generation failed after retry: {last_error}")
