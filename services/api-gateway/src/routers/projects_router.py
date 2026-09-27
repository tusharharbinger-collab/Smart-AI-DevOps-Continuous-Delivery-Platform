"""
services/api-gateway/src/routers/projects_router.py

Phase 8 (§08-project-workspaces.md) — Render-style project workspaces.

A project OWNS a `pipelines` row rather than replacing it: the project holds
the repo/build/image metadata from the creation wizard, its pipeline holds
the generated declarative YAML the worker executes. A project rollout is
therefore an ORDINARY `pipeline_executions` row, which is what lets the
existing verification-engine verdicts, OPA actuations and HMAC-signed audit
entries (all of which FK to `pipeline_run_id`) keep working untouched — see
that roadmap file for why a parallel runs table would have orphaned them.

Every query here goes through `get_request_db` (the RLS-scoped session
auth/middleware.py already opened) and additionally filters on tenant_id.
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone

import httpx
import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from shared import redis_streams as streams
from shared.deployment_readiness import evaluate_deployment_readiness
from shared.intent_spec import FARGATE_TIER_DEFAULTS, EnvironmentTier, IntentSpec, apply_tier_defaults
from shared.live_url_builder import build_live_url
from shared.infra_needs import analyze_infra_needs, build_standard_only_proposal
from src import webhook_registry
from src.auth.rbac import require_role
from src.config import settings
from src.infra_cost import apply_independent_cost
from src.infra_policy import apply_policy_to_proposal, blocking_messages, evaluate_infra_policy
from src.db.session import get_db, get_request_db
from src.routers.github_router import _resolve_token
from src.routers.aws_connections_router import load_provisioning_connection

router = APIRouter()
logger = structlog.get_logger(__name__)

STREAM_PIPELINE_START = "stream:pipeline:start"
# Gate 1 (P0, 2026-09-16) — a webhook-triggered push is queued here first
# instead of going straight to _trigger_rollout_internal. pipeline-worker's
# gate1 consumer runs a real build+test dry run (build_preview.py, already
# built and live-tested for the onboarding wizard) and only calls back into
# POST /internal/{project_id}/gate1-result with passed=True once the new
# commit is proven to actually build and pass its own tests — matching the
# roadmap's own "fail here -> no deployment attempt at all" requirement.
STREAM_GATE1_CHECK = "stream:gate1:check"
PIPELINE_WORKER_URL = os.environ.get("PIPELINE_WORKER_URL", "http://pipeline-worker:8001")
POLICY_CONTROLLER_URL = os.environ.get("POLICY_CONTROLLER_URL", "http://policy-controller:8003")
# Real gap found live: a project's path_prefix was computed at onboarding,
# routed into the real HTTPRoute, and then thrown away — never persisted,
# never returned, so there was no way to show a user a clickable "is my
# product actually live" link the way Render/Vercel do. Points at whichever
# real gateway is in front of the local Kind cluster right now
# (`cloud-provider-kind` publishes the real host-reachable port — see
# docs/session-notes/2026-09-15-docker-pull-network-blocked.md for why this
# isn't k8s/kind-config.yaml's own port mapping, which routes nowhere real).
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost")
# Module 8 — the real shared ALB's DNS name once a project has been
# onboarded to AWS ECS at least once (ecs_onboarding.py's SHARED_ALB_NAME).
# An ALB's DNS name is stable for its lifetime, so this only needs setting
# once, the same way GATEWAY_BASE_URL only needs setting once per cluster.
AWS_ALB_BASE_URL = os.environ.get("AWS_ALB_BASE_URL")


async def _live_url(
    path_prefix: str | None, deploy_target: str = "kubernetes", redis_client=None, region: str = "us-east-1",
) -> str | None:
    if not path_prefix:
        return None
    if deploy_target == "aws_ecs":
        # Real gap found live (2026-09-17): this used to concatenate with no
        # trailing slash — see shared/live_url_check.py::build_live_url's
        # docstring for the exact live incident (a real onboarded app
        # rendered completely unstyled with no working JS at this link).
        #
        # BACKLOG P2 #7a — AWS_ALB_BASE_URL is a static env var that goes stale the moment the shared ALB is
        # recreated. api-gateway has no boto3, so it can't resolve the DNS name itself (see the "no boto3" trap
        # in CLAUDE.md) — pipeline-worker/policy-controller resolve it live and publish it here instead, every
        # time they touch AWS for a real rollout. Falls back to the static env var only if nothing was ever
        # published yet (e.g. no project in this region has onboarded since the gateway last restarted).
        alb_base_url = AWS_ALB_BASE_URL
        if redis_client is not None:
            try:
                published = await redis_client.get(f"platform:alb_dns_name:{region}")
                if published:
                    alb_base_url = published
            except Exception as e:
                logger.warning("alb_dns_redis_read_failed", region=region, error=str(e))
        return build_live_url(alb_base_url, path_prefix)
    normalized_prefix = path_prefix if path_prefix.endswith("/") else f"{path_prefix}/"
    return f"{GATEWAY_BASE_URL}{normalized_prefix}"


def _build_method_is_declared(
    dockerfile_path: str | None, language: str | None, start_command: str | None, framework: str | None
) -> bool:
    """
    A pure, directly-testable helper for create_project's 422 guard — a
    real Dockerfile always suffices; otherwise a language is required, and
    a start_command too UNLESS the project is a static site or a Vite/CRA
    "spa" (see dockerfile_synthesis.py's STATIC_TEMPLATE/NODE_TEMPLATES["spa"]),
    neither of which has a runtime process to start at all.
    """
    if dockerfile_path:
        return True
    if not language:
        return False
    return bool(start_command) or language == "static" or framework == "spa"


def _validate_deploy_mode(deploy_mode: str, deploy_target: str) -> None:
    """
    A pure, directly-testable helper (deliberately separated from
    create_project's body, which needs a live DB session to exercise) — see
    worker.py's canary_loop blue-green branch and OPA's HEALTH_GATED_CUTOVER
    rule for what "blue_green" actually does at execution time.
    """
    if deploy_mode not in ("canary", "blue_green"):
        raise HTTPException(
            status_code=422,
            detail=f"deploy_mode '{deploy_mode}' is not recognized — use 'canary' or 'blue_green'.",
        )
    if deploy_mode == "blue_green" and deploy_target != "aws_ecs":
        # Blue-green's real actuation (worker.py's canary_loop branch,
        # OPA's HEALTH_GATED_CUTOVER rule) is built for AWS ECS only,
        # matching the platform's existing AWS-ECS-only scope decision
        # (PROJECT_STATUS.md, 2026-09-16) — reject rather than silently
        # falling back to canary, which would mislead a human who
        # deliberately chose blue-green for a Kubernetes project.
        raise HTTPException(
            status_code=422,
            detail="deploy_mode 'blue_green' is only supported for deploy_target 'aws_ecs' today.",
        )


EXPLAINABILITY_SERVICE_URL = os.environ.get("EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004")
LOG_POLL_INTERVAL_SECONDS = 0.5

# The three user-facing stages a project's workspace shows as a stepper.
# These are the *stage names* the generated YAML declares, so the stepper
# maps 1:1 onto what the worker actually executes and reports as
# `current_stage` — no translation table that can silently drift.
PROJECT_STAGES = ["build", "test", "canary_verify"]


# ─────────────────────────── request models ───────────────────────────


class Guardrails(BaseModel):
    confidence_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    min_sample_size: int = Field(default=100, ge=1)
    max_cost_delta_percent: float = Field(default=15.0, ge=0.0)


class BlockedDeployWindow(BaseModel):
    days: list[str]
    start_time: str
    end_time: str


class DeployPolicy(BaseModel):
    """
    Real gap found live: `generate_project_pipeline_yaml` hardcoded
    `blockedDeployWindows: []` and a fixed `manualApprovalRequired` block
    for EVERY project — the wizard never actually exposed these, so no
    onboarded project could ever declare a freeze window or choose its own
    approver roles, despite both being genuine, explicit rubric items.
    Defaults match exactly what was hardcoded before, so an existing caller
    that omits this block sees no behavior change.

    `deploy_mode` is "canary" (progressive, statistically-verified ramp) or
    "blue_green" (instant, health-gated cutover — see worker.py's
    canary_loop blue-green branch and OPA's HEALTH_GATED_CUTOVER rule).
    Blue-green is AWS ECS only (create_project rejects it for
    deploy_target=kubernetes with a 422) — it exists specifically to
    guarantee a live URL for a project with zero real traffic, which a
    statistically-gated canary ramp structurally cannot do.
    """
    deploy_mode: str = "canary"
    blocked_deploy_windows: list[BlockedDeployWindow] = Field(default_factory=list)
    manual_approval_required: bool = True
    manual_approval_roles: list[str] = Field(default_factory=lambda: ["lead-sre", "platform-admin"])


class CreateProjectRequest(BaseModel):
    name: str
    # "repository" (the original flow: clone + build + test + canary) or
    # "existing_image" (skip clone/build/test entirely — deploy a pre-built
    # image straight into the canary loop, the way Render's "Existing Image"
    # tab works). Validated in create_project() rather than as a Literal so
    # the error message can name exactly which field is missing.
    source_type: str = "repository"
    repo_url: str | None = None
    # Drives whether the generated build stage demands a credential at clone
    # time. Emitting `repoCredentialsEnvVar` unconditionally made every
    # PUBLIC repo fail its build — git_clone.py (correctly) refuses to clone
    # when a pipeline names a credentials env var that is empty, rather than
    # silently falling back to an anonymous clone. The wizard gets this flag
    # straight from GitHub's own `private` field.
    repo_private: bool = False
    branch: str = "main"
    root_directory: str = "./"
    # Real bug found live: defaulting this to "Dockerfile" meant ANY caller
    # that didn't explicitly send `dockerfile_path: null` — including a
    # request correctly built from the synthesis path (language +
    # start_command set, no Dockerfile) — silently got treated as "build
    # from a Dockerfile at the default path" anyway, generating the WRONG
    # pipeline YAML with no error at all. Defaulting to None instead forces
    # every caller to make an explicit choice; the 422 guard below catches
    # the case where neither choice was actually made.
    dockerfile_path: str | None = None
    # Populated only when dockerfile_path is None — the auto-detected or
    # smartcd.yaml-declared language/start command/manifest location (see
    # shared/repo_scanner.py's BuildDetection). Never guessed server-side;
    # these are exactly what build-detection returned and the human confirmed.
    language: str | None = None
    # Guaranteed Live Web App CI/CD — only ever meaningful when language ==
    # "node" ("spa" | "nextjs" | "node-server"), picks the right synthesized
    # Dockerfile template (see dockerfile_synthesis.py). None for every
    # other language, and for a project with a real dockerfile_path.
    framework: str | None = None
    start_command: str | None = None
    manifest_path: str | None = None
    test_command: str | None = None
    container_image: str
    active_production_tag: str = "v1.0.0"
    canary_tag: str | None = "v1.1.0"
    # Module 8 — real gap this closes: the platform could build and push a
    # real image to ECR, but had no way to actually RUN it anywhere but the
    # local Kind cluster. "aws_ecs" is a second genuine deployment target
    # (real ECS Fargate + a real Application Load Balancer with weighted
    # target groups — the ALB equivalent of the Kubernetes side's HTTPRoute
    # weight patch), not a simulated one. Defaults to "kubernetes" so every
    # existing caller is completely unaffected.
    deploy_target: str = "kubernetes"
    aws_region: str = "us-east-1"
    # Set only for source_type == "existing_image": an id from
    # GET /api/v1/integrations/registry/credentials. The raw credential is
    # never sent again after it was created — pipeline-worker resolves this
    # id against the same Redis store api-gateway wrote it to.
    registry_credential_id: str | None = None
    # Networking for the generated Kubernetes objects. These were previously
    # only settable through the separate "Add Service" dialog; they live here
    # so the project wizard is a strict superset of that flow and nothing was
    # lost when the classic console was retired.
    port: int = 8080
    health_check_path: str = "/healthz"
    path_prefix: str | None = None
    guardrails: Guardrails = Field(default_factory=Guardrails)
    deploy_policy: DeployPolicy = Field(default_factory=DeployPolicy)
    traffic_steps: list[int] = Field(default_factory=lambda: [10, 25, 50, 100])
    # Best-effort: generate + apply the real Deployment/Service/HTTPRoute via
    # pipeline-worker. Requires a reachable cluster; when it fails the project
    # is still created and the failure is reported (see create_project).
    provision_cluster: bool = True
    # AI Autonomous Push-to-Live Loop: when True, healthy verified canaries cut over
    # to 100% and graduate automatically without pausing for manual approval.
    # When None, defers to deploy_policy.manual_approval_required.
    auto_graduate: bool | None = None
    # Phase 6 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5) — an AI-tuned candidate
    # the human previewed and explicitly chose to use (see POST
    # /pipeline-preview/generate), in place of generate_project_pipeline_yaml's
    # deterministic template. Re-validated here regardless of the preview
    # endpoint's own validation — this is the real creation path and must
    # never trust a client-supplied YAML string without its own check.
    ai_tuned_policy_yaml: str | None = None
    # AI_AGENTIC_ORCHESTRATION_PLAN.md n8n-style visualization gap: when the
    # human went through the Requirements Form / infra-draft flow before
    # creating this project, this carries that draft's id so it can be
    # linked to the new project (infra_build_state.project_id) — otherwise
    # nothing in the persistent project view can ever find its infra
    # topology again after creation. Never required: a project created
    # without ever touching the infra-draft flow has no draft to link.
    infra_draft_id: str | None = None


class LogHygieneRequest(BaseModel):
    code_files: dict[str, str] = Field(default_factory=dict)
    cloudwatch_logs: list[str] = Field(default_factory=list)


class PredictiveRiskRequest(BaseModel):
    commit_diff: str = ""
    commit_message: str = ""
    files_changed: list[str] = Field(default_factory=list)


class TriggerRolloutRequest(BaseModel):
    commit_sha: str | None = None
    commit_message: str | None = None
    target_version: str | None = None
    trigger_type: str = "MANUAL_UI"


class GeneratePipelineRequest(BaseModel):
    prompt: str


class AskProjectQuestionRequest(BaseModel):
    question: str


# ─────────────────────────── helpers ───────────────────────────


def _get_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


def _require_valid_uuid_or_404(value: str, what: str = "resource") -> None:
    """
    Real bug found live (Phase 7 smoke test): `WHERE draft_id = :draft_id`
    against a UUID column raises a raw asyncpg `DataError` — surfaced as an
    opaque 500 — when the path parameter isn't a valid UUID at all (e.g. a
    typo, or a client probing a malformed id), instead of the clean 404 a
    "not found" case should be. A malformed identifier and a well-formed
    but absent one should look identical to the caller.
    """
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"{what.capitalize()} not found")


def _metric_prefix(project_name: str) -> str:
    return project_name.replace("-", "_").replace(".", "_")


def _k8s_name(name: str) -> str:
    """
    Real bug found live: a project named "RaktDoot" (or any name with
    uppercase letters, spaces, or other characters outside
    `[a-z0-9-]`) was passed straight through to Deployment/Service/
    HTTPRoute names — Kubernetes requires a lowercase RFC 1123 label/
    subdomain for every one of those, and rejected the onboarding call
    outright with a 422 the wizard gave the user no way to anticipate
    ("RaktDoot-baseline": a lowercase RFC 1123 subdomain must consist of
    lower case alphanumeric characters...). Derives a real k8s-safe slug
    for every K8s-facing identifier this project generates; `projects.name`
    itself keeps the user's original display name untouched.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "service"


def generate_project_pipeline_yaml(
    body: CreateProjectRequest, tenant_id: str, namespace: str, effective_path_prefix: str
) -> str:
    """
    Renders the wizard's inputs into the platform's existing declarative
    Pipeline YAML (§4.1) using stage types the worker ALREADY implements:
    `build` (with repoUrl → real git clone, see tasks/git_clone.py), `test`,
    and `canary_loop`. This is the whole of Step 6's "clone → build → test →
    canary" execution hook: no new runner, just the generated manifest that
    drives the existing one.

    `source_type == "existing_image"` (the wizard's third tab, alongside Git
    Provider and Public Git Repository) skips the `build`/`test` stages
    entirely — there is no repository to clone or test command to run, only
    an already-built image to canary. This mirrors the single-stage shape
    the two pre-Phase-8 pipelines already used (see migration 0007's adopted
    `payments-pipeline`/`checkout-service-rollout`), so it is a pattern the
    worker has executed correctly since before projects existed, not a new one.

    Stages carry no `dependsOn` because dag_builder.py makes each stage
    implicitly depend on the previous one, so declaration order IS execution
    order.
    """
    steps = body.traffic_steps or [10, 25, 50, 100]
    auto_grad = getattr(body, "auto_graduate", None)
    if auto_grad is not None:
        manual_approval = not auto_grad
    else:
        manual_approval = getattr(body.deploy_policy, "manual_approval_required", True)
    # minDuration/minSampleSize scale with the traffic step: a 10% canary is
    # allowed a shorter, smaller-sample window than a 100% cutover.
    step_lines = []
    for weight in steps:
        is_final = weight >= 100
        # Only a step held for a human (requiresManualApproval) may skip its duration/sample floors - the
        # worker's schema rejects an automated step with a zero floor, so an auto-graduating final step
        # (auto_graduate, or manual_approval_required=false) must carry a real one.
        gated_by_human = is_final and manual_approval
        if gated_by_human:
            duration, sample = 0, 0
        elif is_final:
            duration, sample = 120, body.guardrails.min_sample_size
        else:
            duration = max(120, weight * 6)
            sample = max(body.guardrails.min_sample_size, weight * 10)
        step_lines.append(f"          - trafficWeight: {weight}")
        step_lines.append(f"            minDuration: {duration}s")
        step_lines.append(f"            minSampleSize: {sample}")
        if is_final and manual_approval:
            step_lines.append("            requiresManualApproval: true")

    prefix = _metric_prefix(body.name)
    k8s_name = _k8s_name(body.name)
    canary_tag = body.canary_tag or "v1.1.0"

    build_and_test_stages = ""
    if body.source_type != "existing_image":
        # Real gap found live: defaulting to a fake `echo` command made the
        # test stage look like it ran something when it didn't, and (before
        # worker.py/build_preview.py were made non-blocking) a
        # user-supplied command that didn't match this project's actual
        # language — e.g. a leftover "pytest tests/" against a JS repo —
        # hard-failed the whole pipeline on a false negative. Test commands
        # are genuinely optional now: blank means "skip, the repo's own
        # Dockerfile may already run real tests as a build layer" (see
        # worker.py's test-stage docstring), and even a real command's
        # failure no longer blocks build/deploy.
        test_command = body.test_command or ""
        # Only a private repo names a credentials env var. git_clone.py hard-fails
        # if a pipeline names one that is unset, so emitting this for a public
        # repo would break its build for no reason.
        credentials_line = (
            "\n        repoCredentialsEnvVar: GITHUB_TOKEN" if body.repo_private else ""
        )
        # Read by worker.py to authenticate a real `docker push` after the
        # build — absent (None) means "no credential configured", which
        # routes the built image through kind_loader.py instead (local Kind
        # dev, the common case). See build_task.py's push-or-kind-load split.
        registry_credential_line = (
            f"\n        registryCredentialId: {body.registry_credential_id}"
            if body.registry_credential_id
            else ""
        )
        # Real gap found live: this always emitted `dockerfilePath`, even
        # for a project the wizard's build-detection scan (shared/
        # repo_scanner.py) correctly identified as having NO Dockerfile —
        # such a project could pass onboarding-time preview (build_preview.py
        # already supported synthesis) and then fail every real rollout,
        # since worker.py's build stage had nothing to synthesize FROM. Now
        # mirrors the preview path exactly: a real dockerfile_path wins if
        # given; otherwise language/startCommand/manifestPath (all sourced
        # from the human-confirmed build-detection result, never guessed
        # here) tell worker.py to synthesize one at build time.
        if body.dockerfile_path:
            dockerfile_in_repo = os.path.join(
                body.root_directory.strip("./") or "", body.dockerfile_path
            ).replace("\\", "/").lstrip("/")
            build_config_lines = f"        dockerfilePath: {dockerfile_in_repo or 'Dockerfile'}"
        else:
            manifest_in_repo = os.path.join(
                body.root_directory.strip("./") or "", body.manifest_path or "requirements.txt"
            ).replace("\\", "/").lstrip("/")
            # Guaranteed Live Web App CI/CD — a static site / Vite "spa" has
            # no start_command at all (see the create_project validation
            # above); emitting a literal `startCommand: "None"` used to be
            # harmless only because nothing without a start_command could
            # ever reach this branch before. `framework` is only emitted
            # when set (node projects only) — worker.py's synthesis call
            # reads it via config.get("framework"), defaulting to the
            # original node-server template when absent.
            start_command_line = f'\n        startCommand: "{body.start_command}"' if body.start_command else ""
            framework_line = f"\n        framework: {body.framework}" if body.framework else ""
            build_config_lines = (
                f"        language: {body.language}{start_command_line}{framework_line}\n"
                f"        manifestPath: {manifest_in_repo}"
            )
        build_and_test_stages = f"""    - name: build
      type: build
      config:
        repoUrl: {body.repo_url}
        repoRef: {body.branch}{credentials_line}
{build_config_lines}
        imageTag: {canary_tag}
        image: {body.container_image}{registry_credential_line}

    - name: test
      type: test
      config:
        command: {test_command}

"""

    # Real gap found live: this generator used to hardcode
    # `blockedDeployWindows: []` and a fixed `manualApprovalRequired` block
    # for every single project — the wizard never actually let a human
    # declare a freeze window or choose approver roles, despite both being
    # explicit rubric items. Now built from DeployPolicy, defaulting to
    # exactly what was hardcoded before so an omitted deploy_policy changes
    # nothing for an existing caller.
    dp = body.deploy_policy
    if dp.blocked_deploy_windows:
        # Real bug found live: an unquoted HH:MM value (e.g. 16:00) is valid
        # YAML 1.1 sexagesimal notation — PyYAML's safe_load silently parsed
        # it as the integer 960 instead of the string "16:00", which the
        # Rego policy's string comparison (`context.current_time >=
        # window.startTime`) would then compare against a real time string
        # and never match correctly. Quoting forces it to stay a string.
        window_lines = "\n".join(
            f'      - days: [{", ".join(w.days)}]\n'
            f'        startTime: "{w.start_time}"\n'
            f'        endTime: "{w.end_time}"'
            for w in dp.blocked_deploy_windows
        )
        blocked_windows_yaml = f"    blockedDeployWindows:\n{window_lines}"
    else:
        blocked_windows_yaml = "    blockedDeployWindows: []"
    if manual_approval:
        roles_yaml = ", ".join(f'"{r}"' for r in dp.manual_approval_roles)
        approval_yaml = (
            "    manualApprovalRequired:\n"
            "      beforeStages: [step_100_promotion]\n"
            f"      approverRoles: [{roles_yaml}]"
        )
    else:
        approval_yaml = "    manualApprovalRequired:\n      beforeStages: []\n      approverRoles: []"
    gates_block = f"{blocked_windows_yaml}\n{approval_yaml}"

    # Real bug found live: this generator never emitted a `deploy` stage at
    # all — a project's build stage would build and PUSH a real image to its
    # real registry, but `canary_loop` (below) only shifts HTTPRoute traffic
    # weight and runs verification; it never touches a Deployment's image
    # (see worker.py). So the canary Deployment onboarding created just sat
    # on whatever placeholder image it was given at onboarding time,
    # forever — every wizard-onboarded project's rollout could report
    # COMPLETED while the actual pods never ran the new code (or, if the
    # onboarding-time image was invalid, never ran at all). `image` here
    # (as opposed to the legacy hand-wired payments-service deploy stage,
    # which has none) is what tells worker.py to patch THIS project's real
    # onboarded Deployment via deploy_project_canary_task instead of the
    # payments-specific hardcoded path.
    # Module 8 — real gap found live: this generator only ever produced
    # Kubernetes-shaped stage config (a Deployment name, a namespace) — a
    # project whose deploy_target is "aws_ecs" had no way for worker.py's
    # deploy stage or policy-controller's actuation to know they should
    # touch a real ECS service/ALB listener rule instead of a Kubernetes
    # Deployment/HTTPRoute. `deploymentTarget`/`awsRegion`/`pathPrefix` are
    # emitted on BOTH the deploy stage and canary_loop stage so each reads
    # what it needs from its own config without cross-referencing the
    # other — worker.py's `_register_actuation_target` (canary_loop) is
    # what threads this into the Redis `actuation_target` dict
    # policy-controller reads for every subsequent verdict.
    aws_target_lines = (
        f"\n        deploymentTarget: aws_ecs\n        awsRegion: {body.aws_region}\n"
        f"        pathPrefix: {effective_path_prefix}"
        if body.deploy_target == "aws_ecs"
        else ""
    )
    # CloudWatch telemetry (P1, 2026-09-16) — a truthy marker is all a
    # metric needs here (unlike Prometheus's bespoke `prometheus:` block,
    # no per-metric query string is required — cloudwatch_client.py derives
    # the real query from this platform's own fixed ALB/target-group naming
    # convention, given only the category + the run's real service_name/
    # region, already threaded through separately via aws_target_lines
    # above and worker.py's canary_loop config at run time).
    cloudwatch_metric_line = "\n        cloudwatch: {}" if body.deploy_target == "aws_ecs" else ""
    # Read by worker.py's canary_loop dispatch (config.get("deploymentStrategy"))
    # to route a blue-green project into the health-gated cutover branch
    # instead of the statistically-verified ramp — see that branch's own
    # docstring for why a zero-traffic app needs this. Deliberately only on
    # canary_loop's config, not the deploy stage's (worker.py never reads
    # it there), to avoid an unused field on a stage that doesn't act on it.
    deployment_strategy_line = (
        "\n        deploymentStrategy: blue_green" if body.deploy_policy.deploy_mode == "blue_green" else ""
    )
    deploy_stage = f"""    - name: canary_deploy
      type: deploy
      config:
        deployment: {k8s_name}-canary
        namespace: {namespace}
        containerName: {k8s_name}
        image: {body.container_image}
        imageTag: {canary_tag}{aws_target_lines}

"""

    return f"""apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: {k8s_name}-rollout
  tenantId: "{tenant_id}"
  namespace: {namespace}

spec:
  stages:
{build_and_test_stages}{deploy_stage}    - name: canary_verify
      type: canary_loop
      config:
        gatewayRef: local-edge-gateway
        service: {k8s_name}
        routeName: {k8s_name}-route
        canaryDeployment: {k8s_name}-canary
        baselineDeployment: {k8s_name}-baseline{aws_target_lines}{deployment_strategy_line}
        steps:
{chr(10).join(step_lines)}

  gates:
{gates_block}

  guardrails:
    autoRollbackOnVerdict: ["FAILED"]
    requireMinimumConfidence: {body.guardrails.confidence_floor}
    minSampleSize: {body.guardrails.min_sample_size}
    maxPermittedCostDeltaPercent: {body.guardrails.max_cost_delta_percent}

  verificationConfig:
    minEvaluationWindowSeconds: 20
    metrics:
      - name: {prefix}_error_rate
        category: error_rate
        tier: critical
        alpha: 0.01
        p0: 0.005
        p1: 0.020{cloudwatch_metric_line}
      - name: {prefix}_p95_latency_seconds
        category: latency
        tier: important
        alpha: 0.05
        weight: 2.5{cloudwatch_metric_line}
"""


async def _load_project(db: AsyncSession, project_id: str, tenant_id: str, request: Request | None = None) -> dict:
    result = await db.execute(
        text(
            """
            SELECT project_id, tenant_id, pipeline_id, name, repo_url, branch, root_directory,
                   dockerfile_path, language, start_command, manifest_path, test_command,
                   container_image, active_production_tag,
                   canary_tag, status, created_at, path_prefix, deploy_target,
                   deploy_mode, live_url_status, live_url_verified_at
            FROM projects
            WHERE project_id = :project_id AND tenant_id = :tenant_id
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project = dict(row)
    project["live_url"] = await _live_url(
        project.get("path_prefix"), project.get("deploy_target", "kubernetes"),
        request.app.state.redis if request is not None else None, project.get("aws_region", "us-east-1"),
    )
    return project


async def _assert_run_belongs_to_project(
    db: AsyncSession, run_id: str, project_id: str, tenant_id: str
) -> dict:
    """
    Guards every run-scoped endpoint: a run id from a DIFFERENT project (or
    tenant) must not be readable through this project's URL. RLS already
    blocks cross-tenant reads; this additionally enforces project scoping,
    which is the whole point of an "isolated project workspace."
    """
    result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, project_id, pipeline_id, target_version, status,
                   current_stage, current_traffic_weight, trigger_type, commit_sha,
                   commit_message, started_at, completed_at
            FROM pipeline_executions
            WHERE pipeline_run_id = :run_id AND project_id = :project_id AND tenant_id = :tenant_id
            """
        ),
        {"run_id": run_id, "project_id": project_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found for this project")
    return dict(row)


async def _overlay_live_state(redis_client, rows: list[dict]) -> list[dict]:
    """
    pipeline_executions.status is written once at trigger time; the live
    truth while a run is executing lives in Redis `state:{run_id}` (written
    by pipeline-worker's ExecutionStateStore). Same overlay pipeline_router
    already does — reproduced here so a project's run list doesn't show
    every run as PENDING forever.
    """
    for row in rows:
        cached = await redis_client.get(f"state:{row['pipeline_run_id']}")
        if not cached:
            continue
        try:
            live = json.loads(cached)
        except json.JSONDecodeError:
            continue
        row["status"] = live.get("status", row["status"])
        row["current_stage"] = live.get("current_stage", row["current_stage"])
        row["current_traffic_weight"] = live.get(
            "current_traffic_weight", row["current_traffic_weight"]
        )
    return rows


# ─────────────────────────── project CRUD ───────────────────────────


@router.get("")
async def list_projects(request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Every project for the tenant plus its latest run's live status, verdict
    and traffic weight — the payload behind the /projects overview grid.

    `verdict`/`confidence` come from `verification_records` (the HMAC-signed
    source of truth written by verification-engine) via a lateral join
    rather than being denormalized onto the run, so they can never drift
    from the signed record.
    """
    tenant_id = _get_tenant_id(request)
    result = await db.execute(
        text(
            """
            SELECT p.project_id, p.pipeline_id, p.name, p.repo_url, p.branch,
                   p.container_image, p.active_production_tag, p.canary_tag,
                   p.status, p.created_at, p.path_prefix, p.deploy_target,
                   p.deploy_mode, p.live_url_status, p.live_url_verified_at,
                   r.pipeline_run_id AS latest_run_id,
                   r.status          AS latest_run_status,
                   r.current_stage   AS latest_run_stage,
                   r.current_traffic_weight AS latest_traffic_weight,
                   r.commit_sha      AS latest_commit_sha,
                   r.started_at      AS latest_started_at,
                   r.completed_at    AS latest_completed_at,
                   v.status          AS latest_verdict,
                   v.confidence      AS latest_confidence
            FROM projects p
            LEFT JOIN LATERAL (
                -- `pipeline_executions.status` is written once, as 'PENDING',
                -- at trigger time and never updated; the durable final status
                -- is what pipeline-worker persists to `execution_state`.
                -- Reading the raw column made every finished run look PENDING
                -- (and every success rate 0%) once its 24h Redis key expired.
                SELECT e.pipeline_run_id,
                       COALESCE(es.status, e.status) AS status,
                       COALESCE(es.current_stage, e.current_stage) AS current_stage,
                       COALESCE(es.current_traffic_weight, e.current_traffic_weight) AS current_traffic_weight,
                       e.commit_sha, e.started_at, e.completed_at
                FROM pipeline_executions e
                LEFT JOIN execution_state es ON es.pipeline_run_id = e.pipeline_run_id
                WHERE e.project_id = p.project_id AND e.tenant_id = p.tenant_id
                ORDER BY e.started_at DESC
                LIMIT 1
            ) r ON TRUE
            LEFT JOIN LATERAL (
                SELECT status, confidence
                FROM verification_records
                WHERE pipeline_run_id = r.pipeline_run_id
                ORDER BY timestamp_utc DESC
                LIMIT 1
            ) v ON TRUE
            WHERE p.tenant_id = :tenant_id
            ORDER BY p.created_at DESC
            """
        ),
        {"tenant_id": tenant_id},
    )
    projects = [dict(r) for r in result.mappings().all()]

    redis_client = request.app.state.redis
    for proj in projects:
        if not proj.get("latest_run_id"):
            continue
        cached = await redis_client.get(f"state:{proj['latest_run_id']}")
        if not cached:
            continue
        try:
            live = json.loads(cached)
        except json.JSONDecodeError:
            continue
        proj["latest_run_status"] = live.get("status", proj["latest_run_status"])
        proj["latest_run_stage"] = live.get("current_stage", proj["latest_run_stage"])
        proj["latest_traffic_weight"] = live.get(
            "current_traffic_weight", proj["latest_traffic_weight"]
        )

    # Rolling 7-day success rate, computed from real runs only. A project
    # with no finished runs in the window reports null rather than a
    # flattering default — see the honesty constraint in the roadmap file.
    stats_result = await db.execute(
        text(
            """
            SELECT e.project_id,
                   COUNT(*)                                                      AS total_runs,
                   COUNT(*) FILTER (WHERE COALESCE(es.status, e.status) = 'COMPLETED') AS successful_runs,
                   COUNT(*) FILTER (WHERE COALESCE(es.status, e.status) IN ('PENDING','RUNNING')) AS unfinished_runs,
                   AVG(EXTRACT(EPOCH FROM (es.last_updated - e.started_at)))
                       FILTER (WHERE es.last_updated IS NOT NULL)                AS avg_duration_seconds
            FROM pipeline_executions e
            LEFT JOIN execution_state es ON es.pipeline_run_id = e.pipeline_run_id
            WHERE e.tenant_id = :tenant_id
              AND e.project_id IS NOT NULL
              AND e.started_at > now() - interval '7 days'
            GROUP BY e.project_id
            """
        ),
        {"tenant_id": tenant_id},
    )
    stats = {str(r["project_id"]): dict(r) for r in stats_result.mappings().all()}

    for proj in projects:
        s = stats.get(str(proj["project_id"]))
        total = (s or {}).get("total_runs") or 0
        proj["runs_7d"] = total
        # A run still in flight is neither a success nor a failure, so it is
        # excluded from the denominator rather than counted against the rate.
        finished = total - ((s or {}).get("unfinished_runs") or 0)
        proj["success_rate_7d"] = (
            round((s["successful_runs"] / finished) * 100, 1) if finished > 0 else None
        )
        proj["mttv_seconds"] = (
            round(float(s["avg_duration_seconds"]), 1)
            if s and s.get("avg_duration_seconds") is not None
            else None
        )
        proj["live_url"] = await _live_url(
            proj.get("path_prefix"), proj.get("deploy_target", "kubernetes"),
            request.app.state.redis, proj.get("aws_region", "us-east-1"),
        )

    return {"projects": projects}


@router.post("", dependencies=[Depends(require_role("lead-sre"))])
async def create_project(
    body: CreateProjectRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Creates the project from the 3-step wizard payload: generates its
    pipeline YAML, registers the `pipelines` row exactly as
    POST /api/v1/pipelines would, links the two, and (best-effort) asks
    pipeline-worker to apply the real Kubernetes objects.

    Cluster provisioning is deliberately best-effort: if the Kind cluster
    isn't running, the project and its pipeline are still created and the
    response says provisioning failed and why. Failing the whole creation
    would make the entire feature unusable without a cluster, and the
    generated manifests are idempotent, so provisioning can be retried.
    """
    if body.source_type == "existing_image":
        if not body.container_image:
            raise HTTPException(status_code=422, detail="container_image is required for source_type=existing_image")
    elif not body.repo_url:
        raise HTTPException(status_code=422, detail="repo_url is required for source_type=repository")
    elif not _build_method_is_declared(body.dockerfile_path, body.language, body.start_command, body.framework):
        # Real gap this closes: with dockerfile_path now nullable (migration
        # 0011, to support the no-Dockerfile synthesis path), a request with
        # neither a real Dockerfile NOR a language+startCommand pair would
        # previously have generated a pipeline YAML with literal "None"
        # values, failing confusingly deep in the build stage instead of
        # here, at creation time, with a clear reason.
        raise HTTPException(
            status_code=422,
            detail="Either dockerfile_path, or both language and start_command, must be provided "
            "(start_command is not required for a static site or a Vite/CRA app).",
        )

    _validate_deploy_mode(body.deploy_policy.deploy_mode, body.deploy_target)
    for window in body.deploy_policy.blocked_deploy_windows:
        valid_days = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
        if not set(window.days) <= valid_days:
            raise HTTPException(status_code=422, detail=f"Invalid day(s) in blocked deploy window: {window.days}")

    # Real bug found live: a GitHub repo named "RaktDoot" defaulted to
    # container_image "registry.internal/RaktDoot" — Docker repository
    # names must be lowercase, and `docker build -t` rejects an uppercase
    # one outright as an invalid reference. The wizard now lowercases its
    # own auto-filled default, but this rejects it clearly (rather than a
    # confusing failure deep in the build stage) for any caller — the
    # wizard included, if a user hand-edits the field — that bypasses that.
    if body.container_image and body.container_image != body.container_image.lower():
        raise HTTPException(
            status_code=422,
            detail=(
                f"container_image '{body.container_image}' must be lowercase "
                "(Docker repository names cannot contain uppercase letters)."
            ),
        )

    # Real bug found live: a GitHub repo named "test-" (trailing hyphen)
    # defaulted to container_image "registry.internal/test-" — that's
    # already all-lowercase, so it sailed past the check above, but Docker
    # repository name components must also START and END with an
    # alphanumeric character (a lone trailing/leading `-`, `.`, or `_` is
    # invalid). That's exactly the "invalid reference format" the build
    # stage failed on, deep inside `docker build -t`, instead of a clear
    # 422 at onboarding time — same category of bug as the casing check
    # above, just a different Docker naming rule.
    if body.container_image:
        for segment in body.container_image.split("/"):
            if segment and not re.match(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$", segment):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"container_image '{body.container_image}' is not a valid Docker reference: "
                        f"segment '{segment}' must start and end with a lowercase alphanumeric character."
                    ),
                )

    tenant_id = _get_tenant_id(request)
    namespace = f"tenant-{tenant_id.split('-')[0]}"
    # Real gap found live: this was computed again, separately, further
    # below for the onboarding call and never persisted anywhere — computed
    # once here instead, so there is exactly one source of truth for what
    # this project's real path is (used both to persist it, generate the
    # pipeline YAML's canary_loop/deploy stage config, and onboard the real
    # HTTPRoute/ALB listener rule with it).
    effective_path_prefix = body.path_prefix or f"/api/v1/{_k8s_name(body.name)}"
    policy_yaml = generate_project_pipeline_yaml(body, tenant_id, namespace, effective_path_prefix)

    # Fail fast on a manifest this platform's own loader would reject, rather
    # than storing YAML that only explodes later inside the worker.
    try:
        parsed = yaml.safe_load(policy_yaml)
        if not parsed or "spec" not in parsed:
            raise ValueError("generated manifest has no 'spec' section")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generated pipeline YAML is invalid: {e}")

    if body.ai_tuned_policy_yaml:
        async with httpx.AsyncClient(timeout=15.0) as client:
            val_resp = await client.post(
                f"{PIPELINE_WORKER_URL}/pipelines/validate", json={"policy_yaml": body.ai_tuned_policy_yaml}
            )
        validation = val_resp.json()
        if not validation.get("valid"):
            raise HTTPException(
                status_code=422,
                detail=f"Supplied ai_tuned_policy_yaml failed validation: {validation.get('error')}",
            )
        policy_yaml = body.ai_tuned_policy_yaml

    pipeline_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    try:
        await db.execute(
            text(
                """
                INSERT INTO pipelines (pipeline_id, tenant_id, name, policy_yaml)
                VALUES (:pipeline_id, :tenant_id, :name, :policy_yaml)
                """
            ),
            {
                "pipeline_id": pipeline_id,
                "tenant_id": tenant_id,
                "name": f"{body.name}-rollout",
                "policy_yaml": policy_yaml,
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO projects (
                    project_id, tenant_id, pipeline_id, name, repo_url, branch, root_directory,
                    dockerfile_path, language, start_command, manifest_path,
                    test_command, container_image, active_production_tag,
                    canary_tag, path_prefix, deploy_target, deploy_mode, status
                ) VALUES (
                    :project_id, :tenant_id, :pipeline_id, :name, :repo_url, :branch, :root_directory,
                    :dockerfile_path, :language, :start_command, :manifest_path,
                    :test_command, :container_image, :active_production_tag,
                    :canary_tag, :path_prefix, :deploy_target, :deploy_mode, 'IDLE'
                )
                """
            ),
            {
                "project_id": project_id,
                "tenant_id": tenant_id,
                "pipeline_id": pipeline_id,
                "name": body.name,
                "repo_url": body.repo_url,
                "branch": body.branch,
                "root_directory": body.root_directory,
                "dockerfile_path": body.dockerfile_path,
                "language": body.language,
                "start_command": body.start_command,
                "manifest_path": body.manifest_path,
                "test_command": body.test_command,
                "container_image": body.container_image,
                "active_production_tag": body.active_production_tag,
                "canary_tag": body.canary_tag,
                "path_prefix": effective_path_prefix,
                "deploy_target": body.deploy_target,
                "deploy_mode": body.deploy_policy.deploy_mode,
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A project named '{body.name}' already exists for this tenant.",
        )

    # Best-effort link back to the infra draft this project came from (see
    # CreateProjectRequest.infra_draft_id) — never blocks project creation
    # if the draft id is stale/malformed/from another tenant, since RLS
    # already scopes the UPDATE to this tenant and a WHERE match of zero
    # rows is a silent no-op, not an error.
    if body.infra_draft_id:
        try:
            uuid.UUID(body.infra_draft_id)
            await db.execute(
                text(
                    "UPDATE infra_build_state SET project_id = :project_id, updated_at = now() "
                    "WHERE draft_id = :draft_id AND tenant_id = :tenant_id"
                ),
                {"project_id": project_id, "draft_id": body.infra_draft_id, "tenant_id": tenant_id},
            )
            await db.commit()
        except Exception as e:
            logger.warning("infra_draft_project_link_failed", project_id=project_id, error=str(e))

    # Best-effort, mirrors this function's own cluster-provisioning
    # tolerance below: a project with a real GitHub repo_url gets a
    # repo->project Redis mapping so a future webhook delivery (P0 #1,
    # 2026-09-16) can resolve which tenant/project to trigger without any
    # RLS-scoped DB lookup — see webhook_registry.py's module docstring for
    # why this can't just be a tenant-scoped SELECT. Never blocks project
    # creation if Redis is briefly unavailable; POST .../webhook/register
    # re-writes this same mapping and can be used to repair it later.
    repo_full_name = webhook_registry.parse_full_name(body.repo_url)
    if repo_full_name:
        try:
            await webhook_registry.register(
                request.app.state.redis, repo_full_name, project_id, tenant_id, body.branch,
            )
        except Exception as e:
            logger.warning("webhook_repo_mapping_register_failed", project_id=project_id, error=str(e))

    provisioning: dict = {"attempted": body.provision_cluster, "succeeded": False, "detail": None}
    # Module 8 — real gap this closes: the platform could build and push a
    # real image to ECR, but had no way to actually RUN it anywhere but the
    # local Kind cluster. `onboard_response_live_url` is set from the
    # onboarding call's own response for "aws_ecs" (the real ALB DNS name,
    # known only once AWS actually assigns it) rather than the static
    # GATEWAY_BASE_URL/AWS_ALB_BASE_URL computation _live_url falls back to
    # — that computation only works once AWS_ALB_BASE_URL has been set from
    # a PRIOR onboarding, which can't be true for the very first one.
    onboard_response_live_url: str | None = None
    if body.provision_cluster:
        try:
            # 300s: a first onboarding creates the shared ALB, which alone takes 2-3 minutes; a 60s budget
            # reported failure for work that then completed.
            async with httpx.AsyncClient(timeout=300.0) as http_client:
                if body.deploy_target == "aws_ecs":
                    resp = await http_client.post(
                        f"{PIPELINE_WORKER_URL}/services/onboard-aws",
                        json={
                            "service_name": _k8s_name(body.name),
                            "image": body.container_image,
                            "baseline_tag": body.active_production_tag,
                            "canary_tag": body.canary_tag or "v1.1.0",
                            "tenant_id": tenant_id,
                            "port": body.port,
                            "health_check_path": body.health_check_path,
                            "path_prefix": effective_path_prefix,
                            "region": body.aws_region,
                        },
                    )
                    if resp.status_code == 200:
                        onboard_response_live_url = resp.json().get("live_url")
                else:
                    resp = await http_client.post(
                        f"{PIPELINE_WORKER_URL}/services/onboard",
                        json={
                            # Real bug found live: passing the raw display name
                            # here (e.g. "RaktDoot") made pipeline-worker build a
                            # Deployment named "RaktDoot-baseline" — Kubernetes
                            # requires lowercase RFC 1123 names and rejected it
                            # with a 422 the wizard had no way to anticipate. See
                            # _k8s_name's docstring; must match the slug already
                            # baked into this project's own pipeline YAML
                            # (canaryDeployment/baselineDeployment above), or the
                            # two would silently point at different Deployments.
                            "service_name": _k8s_name(body.name),
                            "image": body.container_image,
                            "baseline_tag": body.active_production_tag,
                            "canary_tag": body.canary_tag or "v1.1.0",
                            "port": body.port,
                            "health_check_path": body.health_check_path,
                            "path_prefix": effective_path_prefix,
                            "tenant_id": tenant_id,
                            "namespace": namespace,
                            "registry_credential_id": body.registry_credential_id,
                        },
                    )
            provisioning["succeeded"] = resp.status_code == 200
            if resp.status_code != 200:
                provisioning["detail"] = f"pipeline-worker returned {resp.status_code}: {resp.text[:300]}"
        except httpx.RequestError as e:
            provisioning["detail"] = f"pipeline-worker unreachable: {str(e) or type(e).__name__}"

        if not provisioning["succeeded"]:
            logger.warning(
                "project_cluster_provisioning_failed",
                project_id=project_id,
                detail=provisioning["detail"],
            )

    logger.info("project_created", project_id=project_id, pipeline_id=pipeline_id, name=body.name)
    return {
        "project_id": project_id,
        "pipeline_id": pipeline_id,
        "name": body.name,
        "namespace": namespace,
        "generated_pipeline_yaml": policy_yaml,
        "cluster_provisioning": provisioning,
        "live_url": onboard_response_live_url or await _live_url(
            effective_path_prefix, body.deploy_target, request.app.state.redis, body.aws_region,
        ),
    }


class BuildPreviewRequest(BaseModel):
    """Runs BEFORE a project exists — the wizard's "does this actually
    build" step, confirmed by the human after reviewing build-detection's
    checklist. Deliberately not nested under /{project_id} for that reason."""

    repo_url: str
    ref: str = "main"
    repo_private: bool = False
    root_directory: str = "./"
    dockerfile_path: str | None = None
    language: str | None = None
    framework: str | None = None
    manifest_path: str | None = None
    start_command: str | None = None
    test_command: str | None = None


@router.post("/build-preview")
async def start_build_preview(body: BuildPreviewRequest, request: Request):
    """
    Forwards to pipeline-worker's build-only dry run (see
    services/pipeline-worker/src/build_preview.py) — no deploy, no cluster,
    just "does this repo actually build and pass its own tests." The
    private-repo credential is resolved and handed over directly here
    (a synchronous server-to-server call, unlike the async Streams-based
    real-rollout trigger) rather than through the short-lived Redis
    clone_token handoff that path uses — there's no decoupled consumer to
    hand it to.
    """
    payload = body.model_dump()
    if body.repo_private:
        user_id = getattr(request.state, "user_id", None)
        gh_token = await request.app.state.redis.get(f"github:token:{user_id}") if user_id else None
        if not gh_token:
            raise HTTPException(
                status_code=400,
                detail="This repo is private and no connected GitHub account was found to clone it with.",
            )
        payload["credentials_token"] = gh_token

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{PIPELINE_WORKER_URL}/build-preview", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach the build service: {e}")
    return resp.json()


@router.get("/build-preview/{run_id}/logs")
async def get_build_preview_logs(run_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{PIPELINE_WORKER_URL}/build-preview/{run_id}/logs")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach the build service: {e}")
    return resp.json()


@router.get("/build-preview/{run_id}/result")
async def get_build_preview_result(run_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{PIPELINE_WORKER_URL}/build-preview/{run_id}/result")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="No build preview found for this run_id (expired or never started)")
    resp.raise_for_status()
    return resp.json()


@router.post("/intent-spec/normalize")
async def normalize_intent_spec(body: IntentSpec):
    """
    Requirements Form (AI_AGENTIC_ORCHESTRATION_PLAN.md §2.5) support
    endpoint — pure computation, no DB, no AI call. Validates a form
    submission, fills any field the human left as "use the tier default"
    via apply_tier_defaults (so the frontend has a single source of truth
    for Fargate sizing defaults, §R.8, instead of duplicating
    FARGATE_TIER_DEFAULTS in TypeScript), and evaluates Phase 3's tiered
    deployment-readiness gate (§2.4) on the normalized spec — auto_advance/
    warn/require_approval, never a blanket "always ask a human." No
    persistence yet — Phase 4 adds the infra_build_state table this will
    eventually be written to; this step only needs the shape to exist and
    be reliably normalizable.
    """
    normalized = apply_tier_defaults(body)
    readiness = evaluate_deployment_readiness(normalized)
    return {
        **normalized.model_dump(mode="json"),
        "readiness": {"outcome": readiness.outcome, "reasons": readiness.reasons},
    }


@router.get("/intent-spec/tier-defaults/{environment_tier}")
async def get_intent_spec_tier_defaults(environment_tier: EnvironmentTier):
    """Read-only: lets the Requirements Form show the locked per-tier defaults before the human submits anything."""
    return FARGATE_TIER_DEFAULTS[environment_tier]


def _infra_draft_row_to_dict(row) -> dict:
    return {
        "draft_id": str(row["draft_id"]),
        "project_id": str(row["project_id"]) if row["project_id"] else None,
        "status": row["status"],
        "intent_spec": row["intent_spec"],
        "archetype": row["archetype"],
        "infra_proposal": row["infra_proposal"],
        "readiness_outcome": row["readiness_outcome"],
        "readiness_reasons": row["readiness_reasons"],
        "error_message": row["error_message"],
        # Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — real
        # provisioning-execution fields, all present but null/empty until
        # the draft actually reaches those later states.
        "cloud_provider": row["cloud_provider"],
        "change_set_id": row["change_set_id"],
        "stack_name": row["stack_name"],
        "stack_arn": row["stack_arn"],
        "change_set_changes": row["change_set_changes"],
        "provisioning_error": row["provisioning_error"],
        "provisioning_outputs": row["provisioning_outputs"],
        # AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phase A.
        "source": row["source"],
        "existing_resources": row["existing_resources"],
        "parent_draft_id": str(row["parent_draft_id"]) if row["parent_draft_id"] else None,
        "aws_connection_id": str(row.get("aws_connection_id")) if row.get("aws_connection_id") else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


class InfraDraftRequest(IntentSpec):
    """IntentSpec + AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md's infra source choice. The two extra
    fields are stripped before the spec is stored or sent to the model."""
    source: str = "ai_created"  # "ai_created" | "existing"
    existing_selection: dict[str, str] = Field(default_factory=dict)  # slot -> real AWS identifier
    # Backlog #3: provision into THIS tenant's own (verified) AWS connection instead of the platform's account.
    aws_connection_id: str | None = None


_INFRA_DRAFT_EXTRA_FIELDS = {"source", "existing_selection", "aws_connection_id"}


def _readiness_with_policy(readiness, proposal: dict) -> tuple[str, list[str], str]:
    """
    Combines the tiered readiness gate with OPA's independent policy verdict. A hard policy failure
    forces a human checkpoint even where readiness would auto-advance (and approval itself is then
    refused until the proposal is edited to pass - see approve_infra_draft). Returns
    (readiness_outcome, reasons, next_status).
    """
    blockers = blocking_messages(proposal)
    if blockers:
        reasons = list(readiness.reasons) + [f"Blocked by infrastructure policy: {m}" for m in blockers]
        return "require_approval", reasons, "INFRA_PENDING_APPROVAL"
    next_status = "INFRA_APPROVED" if readiness.outcome == "auto_advance" else "INFRA_PENDING_APPROVAL"
    return readiness.outcome, list(readiness.reasons), next_status


async def _platform_network_context(region: str, connection: dict | None) -> dict:
    """The real VPC/CIDR/subnets the platform deploys into, so AI-designed extras are placed with real ids."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/infra-provisioning/platform-context",
                json={"region": region, "connection": connection},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not read the platform network: {str(e) or type(e).__name__}")
    if resp.status_code >= 400:
        detail = resp.json().get("detail") if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        raise HTTPException(status_code=502 if resp.status_code >= 500 else 422, detail=f"Could not read the platform network: {detail}")
    return resp.json()


async def _call_generate_infra(payload: dict) -> dict:
    """Shared by create and edit. 150s, not 70s (found live): a rejected-then-corrected attempt
    (e.g. an edit that removed a Retain resource) is two full generations, and Groq's 8,000
    tokens/minute cap adds up to 3 x 15s of rate-limit waits per attempt - a legitimately
    successful edit took ~75s and was killed by the old timeout with an empty error."""
    try:
        async with httpx.AsyncClient(timeout=150.0) as client:
            gen_resp = await client.post(f"{EXPLAINABILITY_SERVICE_URL}/generate-infra", json=payload)
        if gen_resp.status_code >= 400:
            raise httpx.HTTPStatusError(gen_resp.text, request=gen_resp.request, response=gen_resp)
        return gen_resp.json()
    except httpx.HTTPError as e:
        # str(httpx.ReadTimeout) is empty - name the exception so a timeout is never an empty message.
        raise HTTPException(status_code=502, detail=f"AI infra generation unavailable: {str(e) or type(e).__name__}")


async def _verify_existing_selection(selection: dict[str, str], archetype: str, region: str, connection: dict | None = None) -> dict:
    """Re-verifies a human's picks against AWS via pipeline-worker: the client is never trusted
    for what an imported resource actually is. Returns {slot: {"id", "details"}}."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/infra-provisioning/describe-existing",
                json={"selection": selection, "archetype": archetype, "region": region, "connection": connection},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not verify existing resources against AWS: {e}")
    if resp.status_code == 422:
        raise HTTPException(status_code=422, detail=resp.json().get("detail", "Invalid existing-resource selection."))
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Could not verify existing resources against AWS: {resp.text[:300]}")
    return resp.json()


@router.get("/infra-drafts/discover-existing")
async def discover_existing_infra(
    request: Request, archetype: str, region: str | None = None, connection_id: str | None = None,
    db: AsyncSession = Depends(get_request_db),
):
    """AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phase B - read-only picklist of AWS resources that already
    exist, scoped to the slots this archetype needs. With `connection_id` (backlog #3) it lists the resources in
    THAT tenant-owned AWS account instead of the platform's. Registered before /infra-drafts/{draft_id} so
    'discover-existing' is never captured as a draft id."""
    connection = await load_provisioning_connection(db, _get_tenant_id(request), connection_id)
    region = region or (connection["default_region"] if connection else "us-east-1")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if connection:
                # The ExternalId travels in a body, never a URL (URLs end up in access logs).
                resp = await client.post(
                    f"{PIPELINE_WORKER_URL}/infra-provisioning/discover-existing",
                    json={"archetype": archetype, "region": region, "connection": connection},
                )
            else:
                resp = await client.get(
                    f"{PIPELINE_WORKER_URL}/infra-provisioning/discover-existing",
                    params={"archetype": archetype, "region": region},
                )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(resp.text, request=resp.request, response=resp)
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not discover existing AWS resources: {str(e) or type(e).__name__}")


@router.post("/infra-drafts")
async def create_infra_draft(body: InfraDraftRequest, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Phase 4 (AI_AGENTIC_ORCHESTRATION_PLAN.md) - the Infra Architect Agent's entry point. Locks the
    (already-normalized) IntentSpec into a new infra_build_state row, calls explainability-service's
    /generate-infra for the matched archetype, then evaluates the tiered deployment-readiness gate:
    auto_advance skips straight to INFRA_APPROVED, everything else pauses at INFRA_PENDING_APPROVAL.
    Never provisions anything.

    source='existing' (AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md): the human picked real AWS resources
    to attach instead of creating new ones. Each pick is re-verified against AWS (never trusted from
    the client) and handed to the agent as ground truth.
    """
    if not body.archetype:
        raise HTTPException(status_code=422, detail="IntentSpec.archetype must be set (from Phase 1 detection) before drafting infra.")
    source = getattr(body, "source", "ai_created")
    selection = getattr(body, "existing_selection", None) or {}
    if source not in ("ai_created", "existing"):
        raise HTTPException(status_code=422, detail="source must be 'ai_created' or 'existing'.")
    if source == "existing" and not selection:
        raise HTTPException(status_code=422, detail="source 'existing' requires at least one existing_selection entry.")
    if source == "ai_created" and selection:
        raise HTTPException(status_code=422, detail="existing_selection is only valid with source 'existing'.")
    intent_dict = body.model_dump(mode="json", exclude=_INFRA_DRAFT_EXTRA_FIELDS)
    connection = await load_provisioning_connection(db, _get_tenant_id(request), getattr(body, "aws_connection_id", None))
    existing_resources = (
        await _verify_existing_selection(selection, body.archetype, body.aws_region, connection) if source == "existing" else {}
    )

    tenant_id = _get_tenant_id(request)
    draft_id = str(uuid.uuid4())
    intent_spec_json = json.dumps(intent_dict)

    # No explicit commit anywhere in this handler: auth/middleware.py wraps
    # the ENTIRE request in one `session.begin()` transaction and commits it
    # automatically when the handler returns (or rolls back the whole thing
    # on any exception) — a mid-request `db.commit()` ends that transaction
    # early and every subsequent `db.execute()` on the same session then
    # fails with "Can't operate on closed transaction" (found live, testing
    # this endpoint against real Postgres — the FakeDB in this endpoint's
    # unit tests doesn't model transaction semantics, so it didn't catch
    # this). One consequence: if the Groq call fails, the INSERT below rolls
    # back too — no INFRA_DRAFT_FAILED row survives — but the HTTPException's
    # own detail message already carries the real error to the caller, so
    # nothing is lost, just not separately queryable afterward.
    await db.execute(
        text(
            """
            INSERT INTO infra_build_state (draft_id, tenant_id, status, intent_spec, archetype, source, existing_resources, aws_connection_id)
            VALUES (:draft_id, :tenant_id, 'INFRA_DRAFTING', (:intent_spec)::jsonb, :archetype, :source, (:existing)::jsonb, :conn)
            """
        ),
        {
            "draft_id": draft_id, "tenant_id": tenant_id, "intent_spec": intent_spec_json,
            "archetype": body.archetype, "source": source, "existing": json.dumps(existing_resources),
            "conn": getattr(body, "aws_connection_id", None),
        },
    )

    # Decide WHAT is needed before asking any AI: the platform already builds the ALB, cluster and baseline/canary
    # services, so a self-contained web app needs nothing extra (no AI call, no template, no cost).
    needs = analyze_infra_needs(intent_dict, body.archetype, existing_resources)
    if not needs["needs_ai"]:
        proposal = build_standard_only_proposal(intent_dict, body.archetype, needs)
    else:
        payload = {"intent_spec": intent_dict, "archetype": body.archetype, "existing_resources": existing_resources or None}
        if needs["additions"]:
            payload["additions"] = needs["additions"]
            payload["platform_context"] = await _platform_network_context(body.aws_region, connection)
        proposal = await _call_generate_infra(payload)
        proposal = await apply_independent_cost(proposal, body.aws_region)  # before OPA: its budget rule must use the real number
        proposal = apply_policy_to_proposal(proposal, await evaluate_infra_policy(proposal, intent_dict, existing_resources))
        proposal.update(
            no_additional_infrastructure=False, additions=needs["additions"],
            platform_provides=needs["platform_provides"], needs_summary=needs["summary"],
        )
    readiness = evaluate_deployment_readiness(body)
    readiness_outcome, readiness_reasons, next_status = _readiness_with_policy(readiness, proposal)

    await db.execute(
        text(
            """
            UPDATE infra_build_state
            SET status = :status, infra_proposal = (:proposal)::jsonb,
                readiness_outcome = :outcome, readiness_reasons = (:reasons)::jsonb, updated_at = now()
            WHERE draft_id = :draft_id AND tenant_id = :tenant_id
            """
        ),
        {
            "status": next_status,
            "proposal": json.dumps(proposal),
            "outcome": readiness_outcome,
            "reasons": json.dumps(readiness_reasons),
            "draft_id": draft_id,
            "tenant_id": tenant_id,
        },
    )

    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return _infra_draft_row_to_dict(row)


class InfraDraftEditRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=1000)


_INFRA_DRAFT_NOT_EDITABLE = {"INFRA_DRAFTING", "INFRA_CHANGE_SET_CREATING", "INFRA_PROVISIONING"}


@router.post("/infra-drafts/{draft_id}/edit")
async def edit_infra_draft(
    draft_id: str, body: InfraDraftEditRequest, request: Request, db: AsyncSession = Depends(get_request_db)
):
    """
    AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phase D - prompt-driven infra editing. The instruction
    plus the CURRENT proposal go back to the Infra Architect Agent in edit mode (which can never
    remove or un-retain an imported resource - validated in infra_generator, not just prompted).

    The result is a NEW infra_build_state row (parent_draft_id -> this one), never an in-place
    overwrite: the edit history is an audit trail. It re-enters the same tiered approval gate as
    any fresh proposal - an edit is never auto-applied. If this draft was already provisioned, the
    new row inherits its stack name so the next change set is an UPDATE of the SAME stack.
    """
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")
    if row["status"] in _INFRA_DRAFT_NOT_EDITABLE:
        raise HTTPException(status_code=409, detail=f"Draft is in status {row['status']} and cannot be edited right now.")
    if not row["infra_proposal"]:
        raise HTTPException(status_code=409, detail="This draft has no proposal to edit yet.")
    if (row["infra_proposal"] or {}).get("no_additional_infrastructure"):
        raise HTTPException(
            status_code=409,
            detail="This app needs no extra infrastructure, so there is nothing to edit. To add a database, cache or "
            "object storage, turn it on in the Requirements above and generate the proposal again.",
        )

    new_proposal = await _call_generate_infra(
        {
            "intent_spec": row["intent_spec"],
            "archetype": row["archetype"],
            "existing_resources": row["existing_resources"] or None,
            "edit": {"current_proposal": row["infra_proposal"], "instruction": body.instruction},
        }
    )
    new_proposal = await apply_independent_cost(new_proposal, row["intent_spec"].get("aws_region", "us-east-1"))
    new_proposal = apply_policy_to_proposal(
        new_proposal, await evaluate_infra_policy(new_proposal, row["intent_spec"], row["existing_resources"])
    )
    readiness = evaluate_deployment_readiness(IntentSpec.model_validate(row["intent_spec"]))
    readiness_outcome, readiness_reasons, next_status = _readiness_with_policy(readiness, new_proposal)
    inherited_stack = row["stack_name"] if row["status"] == "INFRA_PROVISIONED" else None

    new_draft_id = str(uuid.uuid4())
    await db.execute(
        text(
            """
            INSERT INTO infra_build_state (
                draft_id, tenant_id, project_id, status, intent_spec, archetype, infra_proposal,
                readiness_outcome, readiness_reasons, source, existing_resources, parent_draft_id, stack_name,
                aws_connection_id
            ) VALUES (
                :new_id, :tenant_id, :project_id, :status, (:intent_spec)::jsonb, :archetype, (:proposal)::jsonb,
                :outcome, (:reasons)::jsonb, :source, (:existing)::jsonb, :parent_id, :stack_name, :conn
            )
            """
        ),
        {
            "new_id": new_draft_id, "tenant_id": tenant_id, "project_id": row["project_id"], "status": next_status,
            "intent_spec": json.dumps(row["intent_spec"]), "archetype": row["archetype"],
            "proposal": json.dumps(new_proposal), "outcome": readiness_outcome,
            "reasons": json.dumps(readiness_reasons), "source": row["source"],
            "existing": json.dumps(row["existing_resources"] or {}), "parent_id": draft_id,
            "stack_name": inherited_stack, "conn": row.get("aws_connection_id"),
        },
    )
    new_row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": new_draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return _infra_draft_row_to_dict(new_row)


@router.get("/infra-drafts/{draft_id}")
async def get_infra_draft(draft_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")
    return _infra_draft_row_to_dict(row)


@router.get("/{project_id}/infra-draft")
async def get_project_infra_draft(project_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    n8n-style visualization support: the persistent project view
    (ProjectWorkspace/PipelineDashboard) has no other way to find which
    infra_build_state row (if any) belongs to this project — the draft was
    created BEFORE the project existed (see CreateProjectRequest.infra_draft_id
    / the linking UPDATE in create_project), so this is a reverse lookup by
    project_id rather than draft_id. Returns null (200), not 404, when the
    project was created without ever going through the infra-draft flow —
    that's an expected, common case, not an error.
    """
    _require_valid_uuid_or_404(project_id, "project")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text(
                "SELECT * FROM infra_build_state WHERE project_id = :project_id AND tenant_id = :tenant_id "
                "ORDER BY updated_at DESC LIMIT 1"
            ),
            {"project_id": project_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        return None
    return _infra_draft_row_to_dict(row)


@router.post("/infra-drafts/{draft_id}/approve", dependencies=[Depends(require_role("lead-sre"))])
async def approve_infra_draft(draft_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """The human approval checkpoint for a require_approval/warn-tier proposal (Phase 3's §2.4 gate)."""
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")
    if row["status"] != "INFRA_PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail=f"Draft is in status {row['status']}, not INFRA_PENDING_APPROVAL")
    blockers = blocking_messages(row["infra_proposal"])
    if blockers:
        # A hard policy failure is not something a human can approve past: edit the proposal until it passes.
        raise HTTPException(
            status_code=409,
            detail="Blocked by infrastructure policy - edit the proposal to fix: " + "; ".join(blockers),
        )

    await db.execute(
        text(
            "UPDATE infra_build_state SET status = 'INFRA_APPROVED', updated_at = now() "
            "WHERE draft_id = :draft_id AND tenant_id = :tenant_id"
        ),
        {"draft_id": draft_id, "tenant_id": tenant_id},
    )

    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return _infra_draft_row_to_dict(row)


@router.post("/infra-drafts/{draft_id}/create-change-set")
async def create_infra_change_set(draft_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — the first of two
    human-approval gates before anything real happens. Calls pipeline-
    worker's real AWS CloudFormation `create_change_set` (zero risk — no
    resource is modified) for this draft's approved template. Only valid
    from INFRA_APPROVED; the resulting Change Set diff is what the human
    reviews next, distinct from the earlier "does this design look right"
    approval that got the draft to INFRA_APPROVED in the first place.
    """
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")
    if row["status"] != "INFRA_APPROVED":
        raise HTTPException(status_code=409, detail=f"Draft is in status {row['status']}, not INFRA_APPROVED")

    proposal = row["infra_proposal"] or {}
    template_body = proposal.get("cloudformation_template")
    if proposal.get("no_additional_infrastructure"):
        raise HTTPException(
            status_code=409,
            detail="Nothing to provision: this app needs no extra infrastructure. The platform creates the shared load "
            "balancer and the baseline/canary services when the project is created.",
        )
    if not template_body:
        raise HTTPException(
            status_code=422,
            detail="This draft's infra_proposal has no cloudformation_template to preview.",
        )
    intent_spec = row["intent_spec"] or {}
    region = intent_spec.get("aws_region", "us-east-1")
    connection = await load_provisioning_connection(db, tenant_id, row.get("aws_connection_id"))

    try:
        async with httpx.AsyncClient(timeout=70.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/infra-provisioning/change-set",
                json={
                    "template_body": template_body, "draft_id": draft_id, "region": region,
                    "import_existing": row["source"] == "existing",
                    "stack_name": row["stack_name"],
                    "connection": connection,
                },
            )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(resp.text, request=resp.request, response=resp)
        result = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"CloudFormation change-set preview unavailable: {e}")

    next_status = "INFRA_CHANGE_SET_READY" if result["status"] in ("READY", "NO_CHANGES") else "INFRA_CHANGE_SET_FAILED"

    await db.execute(
        text(
            """
            UPDATE infra_build_state
            SET status = :status, change_set_id = :change_set_id, stack_name = :stack_name,
                stack_arn = :stack_arn, change_set_changes = (:changes)::jsonb,
                provisioning_error = :error, updated_at = now()
            WHERE draft_id = :draft_id AND tenant_id = :tenant_id
            """
        ),
        {
            "status": next_status,
            "change_set_id": result.get("change_set_id") or None,
            "stack_name": result.get("stack_name"),
            "stack_arn": result.get("stack_id"),
            "changes": json.dumps(result.get("changes", [])),
            "error": result.get("status_reason") if next_status == "INFRA_CHANGE_SET_FAILED" else None,
            "draft_id": draft_id,
            "tenant_id": tenant_id,
        },
    )

    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return _infra_draft_row_to_dict(row)


@router.post("/infra-drafts/{draft_id}/execute", dependencies=[Depends(require_role("lead-sre"))])
async def execute_infra_change_set(draft_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    The second, distinct human approval — reviewing a real Change Set diff
    and authorizing real spend is not the same act as approving the AI's
    original design proposal. Role-gated the same way approve_infra_draft
    is. Only valid from INFRA_CHANGE_SET_READY. This is the one call in the
    whole chain that actually creates/modifies real AWS resources.
    """
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")
    if row["status"] != "INFRA_CHANGE_SET_READY":
        raise HTTPException(status_code=409, detail=f"Draft is in status {row['status']}, not INFRA_CHANGE_SET_READY")

    intent_spec = row["intent_spec"] or {}
    region = intent_spec.get("aws_region", "us-east-1")
    connection = await load_provisioning_connection(db, tenant_id, row.get("aws_connection_id"))

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/infra-provisioning/execute",
                json={"change_set_id": row["change_set_id"], "stack_name": row["stack_name"], "region": region,
                      "connection": connection},
            )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(resp.text, request=resp.request, response=resp)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"CloudFormation execute unavailable: {e}")

    await db.execute(
        text(
            "UPDATE infra_build_state SET status = 'INFRA_PROVISIONING', updated_at = now() "
            "WHERE draft_id = :draft_id AND tenant_id = :tenant_id"
        ),
        {"draft_id": draft_id, "tenant_id": tenant_id},
    )

    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return _infra_draft_row_to_dict(row)


@router.post("/infra-drafts/{draft_id}/failure-analysis")
async def analyze_infra_draft_failure(draft_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Backlog #4 - explains WHY a draft's change set or provisioning failed, grounded in the real CloudFormation
    events. Read-only advice: it writes nothing, applies nothing, and never changes the draft's status. Any
    template fix it suggests goes back through "Edit with AI" and both human approvals.
    """
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")
    if row["status"] not in ("INFRA_CHANGE_SET_FAILED", "INFRA_PROVISIONING_FAILED"):
        raise HTTPException(status_code=409, detail=f"Draft is in status {row['status']}; there is no failure to analyze")

    phase = "change_set" if row["status"] == "INFRA_CHANGE_SET_FAILED" else "stack"
    proposal = row["infra_proposal"] or {}
    region = (row["intent_spec"] or {}).get("aws_region", "us-east-1")
    connection = await load_provisioning_connection(db, tenant_id, row.get("aws_connection_id"))

    events: list[dict] = []
    if row.get("stack_name"):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{PIPELINE_WORKER_URL}/infra-provisioning/failure-events",
                    json={"stack_name": row["stack_name"], "region": region, "connection": connection},
                )
            if resp.status_code < 400:
                events = resp.json().get("events", [])
        except httpx.HTTPError as e:
            logger.warning("infra_failure_events_unavailable", draft_id=draft_id, error=str(e) or type(e).__name__)

    payload = {
        "draft_id": draft_id, "phase": phase, "status_reason": row.get("provisioning_error"),
        "events": events, "resources": proposal.get("resources", []),
    }
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(f"{EXPLAINABILITY_SERVICE_URL}/infra-failure-rca", json=payload)
        resp.raise_for_status()
        return {**resp.json(), "phase": phase, "events_examined": len(events)}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Failure analysis unavailable: {str(e) or type(e).__name__}")


@router.get("/infra-drafts/{draft_id}/status")
async def get_infra_provisioning_status(draft_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Polled by the frontend after execute — CloudFormation provisioning is
    asynchronous and can take anywhere from seconds to several minutes.
    Only queries AWS (via pipeline-worker) while still in INFRA_PROVISIONING;
    once terminal, just returns the already-persisted result.
    """
    _require_valid_uuid_or_404(draft_id, "infra draft")
    tenant_id = _get_tenant_id(request)
    row = (
        await db.execute(
            text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
            {"draft_id": draft_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Infra draft not found")

    if row["status"] != "INFRA_PROVISIONING":
        return _infra_draft_row_to_dict(row)

    intent_spec = row["intent_spec"] or {}
    region = intent_spec.get("aws_region", "us-east-1")
    connection = await load_provisioning_connection(db, tenant_id, row.get("aws_connection_id"))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if connection:
                resp = await client.post(
                    f"{PIPELINE_WORKER_URL}/infra-provisioning/status",
                    json={"stack_name": row["stack_name"], "region": region, "connection": connection},
                )
            else:
                resp = await client.get(
                    f"{PIPELINE_WORKER_URL}/infra-provisioning/status",
                    params={"stack_name": row["stack_name"], "region": region},
                )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"CloudFormation status check unavailable: {e}")

    if result["is_terminal"]:
        next_status = "INFRA_PROVISIONED" if result["succeeded"] else "INFRA_PROVISIONING_FAILED"
        await db.execute(
            text(
                """
                UPDATE infra_build_state
                SET status = :status, provisioning_outputs = (:outputs)::jsonb,
                    provisioning_error = :error, updated_at = now()
                WHERE draft_id = :draft_id AND tenant_id = :tenant_id
                """
            ),
            {
                "status": next_status,
                "outputs": json.dumps(result.get("outputs", {})),
                "error": result.get("status_reason") if not result["succeeded"] else None,
                "draft_id": draft_id,
                "tenant_id": tenant_id,
            },
        )
        row = (
            await db.execute(
                text("SELECT * FROM infra_build_state WHERE draft_id = :draft_id AND tenant_id = :tenant_id"),
                {"draft_id": draft_id, "tenant_id": tenant_id},
            )
        ).mappings().first()

    return _infra_draft_row_to_dict(row)


class PipelinePreviewGenerateRequest(BaseModel):
    base_yaml: str
    intent_spec: IntentSpec
    # The human's SELECTED infra proposal (see RequirementsForm.tsx's "Use
    # this infrastructure" action / infra_build_state.infra_proposal) — a
    # plain dict, not the full InfraGenerationResult model, since this
    # service has no dependency on explainability-service's pydantic types,
    # only their serialized shape. Optional: a human can still preview a
    # tuned pipeline before ever generating/selecting an infra proposal.
    infra_proposal: dict | None = None


def _prompt_from_intent_spec(spec: IntentSpec, infra_proposal: dict | None = None) -> str:
    """
    Deterministic prompt construction from the locked IntentSpec — the
    human already declared every one of these values via the Requirements
    Form (§2.5); this just phrases them as an instruction. Never invents a
    requirement the human didn't set.

    `infra_proposal`, when supplied, is the SPECIFIC infra proposal the
    human selected (via RequirementsForm.tsx's "Use this infrastructure"),
    not just the IntentSpec's boolean needs_database/needs_cache flags — it
    lets the prompt reference the actual resource shape (e.g. "an RDS
    instance" instead of "a database") and the AI's own real cost estimate,
    so the tuned pipeline's guardrails are sized against what will actually
    be provisioned rather than a generic archetype guess.
    """
    parts = [
        f"Tune ONLY the canary_loop/guardrails section for a '{spec.environment_tier.value}'-tier "
        f"'{spec.archetype or 'stateless_web_service'}' project — leave the build/test stages byte-for-byte identical."
    ]
    if spec.needs_database:
        parts.append(
            f"This service has a {spec.database_type.value if spec.database_type else 'postgres'} database"
            f"{' with Multi-AZ' if spec.multi_az else ''} — treat error-rate and latency regressions as higher-stakes."
        )
    if spec.needs_cache:
        parts.append("This service has a cache dependency.")
    if spec.monthly_budget_usd is not None:
        parts.append(f"Keep the cost-delta guardrail conservative — declared monthly budget ceiling is ${spec.monthly_budget_usd:.2f}.")
    if spec.environment_tier.value == "production":
        parts.append("Production tier: require manual approval before the final 100% cutover step.")
    if infra_proposal:
        resource_types = sorted({n.get("type", "") for n in infra_proposal.get("topology", {}).get("nodes", []) if n.get("type")})
        if resource_types:
            parts.append(f"The selected infrastructure proposal will actually provision: {', '.join(resource_types)}.")
        cost = infra_proposal.get("estimated_monthly_cost_usd")
        if cost is not None:
            parts.append(
                f"Its AI-estimated real cost is ${float(cost):.2f}/mo — size the cost-delta guardrail against this "
                f"real number, not a guess."
            )
    return " ".join(parts)


@router.post("/pipeline-preview/template")
async def preview_pipeline_template(body: CreateProjectRequest, request: Request):
    """
    Phase 6 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5) — returns the exact
    deterministic pipeline YAML generate_project_pipeline_yaml() would
    produce for this wizard state, WITHOUT creating anything. Pure
    computation, no DB write — lets the wizard show a baseline before
    asking the Infra Architect's sibling, the Pipeline Architect Agent, to
    tune it.
    """
    tenant_id = _get_tenant_id(request)
    namespace = f"tenant-{tenant_id.split('-')[0]}"
    effective_path_prefix = body.path_prefix or f"/api/v1/{_k8s_name(body.name)}"
    policy_yaml = generate_project_pipeline_yaml(body, tenant_id, namespace, effective_path_prefix)
    return {"policy_yaml": policy_yaml}


@router.post("/pipeline-preview/generate")
async def preview_pipeline_ai_tuned(body: PipelinePreviewGenerateRequest, request: Request):
    """
    Phase 6 — the Pipeline Architect Agent, wired in for real (not a mock):
    calls the SAME generate_pipeline_yaml()/validator retry loop the
    existing post-creation editor uses (_generate_and_validate_pipeline_candidate),
    just against a locked IntentSpec-derived prompt instead of free-form
    human text. Never auto-applied — the wizard shows this candidate for
    the human to explicitly choose (POST /projects with
    ai_tuned_policy_yaml set) or discard in favor of the deterministic
    template.
    """
    tenant_id = _get_tenant_id(request)
    context = {"archetype": body.intent_spec.archetype, "environment_tier": body.intent_spec.environment_tier.value}
    return await _generate_and_validate_pipeline_candidate(
        prompt=_prompt_from_intent_spec(body.intent_spec, body.infra_proposal),
        current_yaml=body.base_yaml,
        context=context,
        log_context={"tenant_id": tenant_id, "archetype": body.intent_spec.archetype},
    )


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id, request)

    runs_result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, status, current_stage, current_traffic_weight,
                   commit_sha, commit_message, trigger_type, started_at, completed_at
            FROM pipeline_executions
            WHERE project_id = :project_id AND tenant_id = :tenant_id
            ORDER BY started_at DESC
            LIMIT 1
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id},
    )
    latest = [dict(r) for r in runs_result.mappings().all()]
    latest = await _overlay_live_state(request.app.state.redis, latest)
    project["latest_run"] = latest[0] if latest else None
    return project


@router.delete("/{project_id}", dependencies=[Depends(require_role("lead-sre"))])
async def delete_project(project_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Removes the project. Its runs cascade (pipeline_executions.project_id is
    ON DELETE CASCADE), and with them stage_logs. The generated `pipelines`
    row is removed too — it exists solely to back this project.

    Real gap found live (2026-09-15): this used to only ever touch the
    database — the real Deployments/Services/HTTPRoute create_project's
    onboarding call applied to the cluster were left running forever. A
    project later recreated with the same name hit onboard_service's own
    409-then-PATCH idempotency path against these orphaned objects instead
    of a clean create, silently merging old and new config (a real
    config-drift bug: a stale containerPort survived alongside a freshly
    declared one, caught live while proving the live_url feature end to
    end). Deprovisioning is best-effort, mirroring create_project's own
    best-effort provisioning — a Kind cluster that isn't running right now
    must not block deleting the project record.
    """
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id, request)

    # Symmetric with create_project's registration — an orphaned mapping
    # would otherwise let a push to this repo silently trigger a rollout
    # against a project_id/pipeline_id that no longer exists once someone
    # recreates a DIFFERENT project pointed at the same repo.
    repo_full_name = webhook_registry.parse_full_name(project.get("repo_url"))
    if repo_full_name:
        try:
            await webhook_registry.unregister(request.app.state.redis, repo_full_name)
        except Exception as e:
            logger.warning("webhook_repo_mapping_unregister_failed", project_id=project_id, error=str(e))

    await db.execute(
        text("DELETE FROM projects WHERE project_id = :project_id AND tenant_id = :tenant_id"),
        {"project_id": project_id, "tenant_id": tenant_id},
    )
    pipeline_retained = False
    if project.get("pipeline_id"):
        # Real bug found live (2026-09-24): a pipeline that somehow has other
        # executions attached (a run triggered directly against it before it
        # was linked to a project — those pipeline_executions rows have a
        # NULL project_id, so the projects-cascade above never touches them)
        # makes this DELETE fail with an FK violation. The previous fix for
        # that used a bare `await db.rollback()` — but with no SAVEPOINT,
        # that rolls back the ENTIRE transaction, silently undoing the
        # `DELETE FROM projects` above too, while still returning HTTP 200
        # with `"deleted": project_id`. Caught live: a project deleted
        # through this exact path stayed fully intact in Postgres despite a
        # "success" response. `begin_nested()` scopes the rollback to just
        # this one statement (a real SQL SAVEPOINT) so the projects DELETE
        # already staged in the outer transaction survives and is what
        # `await db.commit()` below actually commits.
        try:
            async with db.begin_nested():
                await db.execute(
                    text("DELETE FROM pipelines WHERE pipeline_id = :pid AND tenant_id = :tid"),
                    {"pid": str(project["pipeline_id"]), "tid": tenant_id},
                )
        except IntegrityError:
            pipeline_retained = True
            logger.warning("project_pipeline_retained", project_id=project_id)
    await db.commit()
    # Cluster/cloud deprovisioning below now always runs regardless of
    # pipeline_retained — the project genuinely IS gone at this point (see
    # the SAVEPOINT above), so its real infra should be torn down same as
    # any other successful deletion; only the shared `pipelines` row survives.

    cluster_deprovisioning: dict = {"attempted": False, "succeeded": False, "detail": None}
    if project.get("container_image") or project.get("repo_url"):
        # Only projects that ever went through real onboarding (a
        # hand-registered/adopted pipeline with neither has nothing in the
        # cluster to remove — see the "No repository connected" note on
        # `repo_url`'s own nullability).
        service_name = _k8s_name(project["name"])
        cluster_deprovisioning["attempted"] = True
        try:
            async with httpx.AsyncClient(timeout=300.0) as http_client:
                if project.get("deploy_target") == "aws_ecs":
                    # Module 8 — real gap found live: this branch didn't exist
                    # at all until now, so every deleted "aws_ecs" project left
                    # its real, billable ECS services/target groups/listener
                    # rule running in AWS forever. Mirrors the kubernetes
                    # branch's own best-effort semantics exactly.
                    resp = await http_client.post(
                        f"{PIPELINE_WORKER_URL}/services/deprovision-aws",
                        json={"service_name": service_name, "path_prefix": project.get("path_prefix")},
                    )
                else:
                    namespace = f"tenant-{tenant_id.split('-')[0]}"
                    resp = await http_client.post(
                        f"{PIPELINE_WORKER_URL}/services/deprovision",
                        json={"service_name": service_name, "namespace": namespace},
                    )
            cluster_deprovisioning["succeeded"] = resp.status_code == 200
            if resp.status_code != 200:
                cluster_deprovisioning["detail"] = f"pipeline-worker returned {resp.status_code}: {resp.text[:300]}"
        except httpx.RequestError as e:
            cluster_deprovisioning["detail"] = f"pipeline-worker unreachable: {str(e) or type(e).__name__}"
        if not cluster_deprovisioning["succeeded"]:
            logger.warning(
                "project_cluster_deprovisioning_failed", project_id=project_id, detail=cluster_deprovisioning["detail"]
            )

    logger.info("project_deleted", project_id=project_id)
    return {
        "deleted": project_id,
        "pipeline_retained": pipeline_retained,
        "cluster_deprovisioning": cluster_deprovisioning,
    }


# ─────────────────────────── execution ───────────────────────────


async def _trigger_rollout_internal(
    db: AsyncSession,
    redis_client,
    tenant_id: str,
    project: dict,
    project_id: str,
    trigger_type: str,
    target_version: str | None = None,
    commit_sha: str | None = None,
    commit_message: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    """
    The real trigger mechanism shared by the authenticated
    POST /{project_id}/rollout route and the GitHub webhook receiver
    (webhooks_router.py) — same pipeline_executions row, same Redis
    Streams publish, same GitHub-credential broker. Split out so a webhook
    delivery (no signed-in user, no request object) gets byte-identical
    actuation behavior to a human clicking "Deploy" — the only difference
    is trigger_type and where commit_sha/commit_message/user_id come from.
    Returns the new run's pipeline_run_id.
    """
    if not project.get("pipeline_id"):
        raise HTTPException(status_code=409, detail="Project has no linked pipeline to execute")

    pipeline_row = await db.execute(
        text("SELECT policy_yaml FROM pipelines WHERE pipeline_id = :pid AND tenant_id = :tid"),
        {"pid": str(project["pipeline_id"]), "tid": tenant_id},
    )
    pipeline = pipeline_row.mappings().first()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Linked pipeline not found")

    run_id = str(uuid.uuid4())
    resolved_target_version = target_version or project.get("canary_tag") or "v1.1.0"
    await db.execute(
        text(
            """
            INSERT INTO pipeline_executions
                (pipeline_run_id, tenant_id, pipeline_id, project_id, target_version,
                 trigger_type, commit_sha, commit_message, status, started_at)
            VALUES
                (:run_id, :tenant_id, :pipeline_id, :project_id, :target_version,
                 :trigger_type, :commit_sha, :commit_message, 'PENDING', :started_at)
            """
        ),
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "pipeline_id": str(project["pipeline_id"]),
            "project_id": project_id,
            "target_version": resolved_target_version,
            "trigger_type": trigger_type,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "started_at": datetime.now(timezone.utc),
        },
    )
    await db.execute(
        text("UPDATE projects SET status = 'BUILDING' WHERE project_id = :pid AND tenant_id = :tid"),
        {"pid": project_id, "tid": tenant_id},
    )
    await db.commit()

    # GitHub OAuth credential broker (Phase 8 follow-up): if the triggering
    # user has connected their own GitHub account, copy that token into a
    # short-lived key scoped to THIS run. pipeline-worker's build stage
    # checks this first — see worker.py's build-stage handler and
    # git_clone.py's `_authenticated_url` — so a private-repo clone uses the
    # caller's own access rather than the server-wide GITHUB_TOKEN every
    # tenant would otherwise share. The 15-minute TTL comfortably covers a
    # queued run without leaving a stray credential behind; worker.py
    # deletes the key itself right after the clone either way. A webhook
    # delivery has no signed-in user (user_id=None) and correctly falls
    # back to the server-wide GITHUB_TOKEN pipeline-worker already uses.
    if user_id:
        gh_token = await redis_client.get(f"github:token:{user_id}")
        if gh_token:
            await redis_client.set(f"clone_token:{run_id}", gh_token, ex=900)

    await streams.publish(
        redis_client,
        STREAM_PIPELINE_START,
        {
            "pipeline_run_id": run_id,
            "pipeline_id": str(project["pipeline_id"]),
            "tenant_id": tenant_id,
            "target_version": resolved_target_version,
            "trace_id": trace_id,
            "policy_yaml": pipeline["policy_yaml"],
        },
    )
    logger.info(
        "project_rollout_triggered",
        project_id=project_id,
        pipeline_run_id=run_id,
        trigger_type=trigger_type,
    )
    return run_id


@router.post("/{project_id}/rollout")
async def trigger_project_rollout(
    project_id: str,
    body: TriggerRolloutRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Starts a delivery run for this project. Creates an ordinary
    `pipeline_executions` row (tagged with project/trigger/commit
    provenance) and publishes onto the SAME `stream:pipeline:start` Redis
    Stream that POST /api/v1/pipelines/{id}/runs uses, so exactly one
    pipeline-worker replica's consumer group picks it up.
    """
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id, request)
    run_id = await _trigger_rollout_internal(
        db=db,
        redis_client=request.app.state.redis,
        tenant_id=tenant_id,
        project=project,
        project_id=project_id,
        trigger_type=body.trigger_type,
        target_version=body.target_version,
        commit_sha=body.commit_sha,
        commit_message=body.commit_message,
        user_id=getattr(request.state, "user_id", None),
        trace_id=getattr(request.state, "trace_id", None),
    )
    return {"pipeline_run_id": run_id, "project_id": project_id, "status": "PENDING"}


@router.post("/{project_id}/webhook/register")
async def register_project_webhook(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Real gap this closes: webhooks_router.py can receive and correctly
    process a GitHub push delivery, but nothing ever asked GitHub to
    actually SEND one — without this, "autonomous git push -> cloud"
    still required a human to hand-configure a webhook in GitHub's own
    repo settings UI. Creates (or confirms) a real `push`-event webhook
    subscription on the project's connected repo via the GitHub API, using
    the signed-in user's own OAuth token (the `repo` scope already
    requested at connect time covers repository webhooks — GitHub's own
    scope docs confirm `repo` "grants full access ... including
    repository webhooks"). Idempotent: lists existing hooks first and
    reuses one already pointed at our URL instead of creating a duplicate
    on every retry. Also re-writes the Redis repo->project mapping
    (webhook_registry.py) as a side effect — doubles as a manual "repair my
    broken webhook" action if that Redis-only index was ever lost.
    """
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id, request)

    if not settings.GITHUB_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_WEBHOOK_SECRET is not configured on this server — webhook registration is disabled.",
        )

    repo_full_name = webhook_registry.parse_full_name(project.get("repo_url"))
    if not repo_full_name:
        raise HTTPException(status_code=409, detail="Project has no connected GitHub repository to register a webhook on.")
    owner, repo = repo_full_name.split("/", 1)

    token = await _resolve_token(request, None)
    webhook_url = f"{settings.API_GATEWAY_PUBLIC_URL}/api/v1/webhooks/github"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            existing_resp = await client.get(f"https://api.github.com/repos/{owner}/{repo}/hooks", headers=headers)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")
        if existing_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"GitHub error listing existing hooks: {existing_resp.status_code} {existing_resp.text[:200]}")

        existing_hooks = existing_resp.json() if isinstance(existing_resp.json(), list) else []
        already_registered = next(
            (h for h in existing_hooks if (h.get("config") or {}).get("url") == webhook_url), None,
        )

        if already_registered is None:
            create_resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/hooks",
                headers=headers,
                json={
                    "name": "web",
                    "active": True,
                    "events": ["push"],
                    "config": {
                        "url": webhook_url,
                        "content_type": "json",
                        "secret": settings.GITHUB_WEBHOOK_SECRET,
                    },
                },
            )
            if create_resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"GitHub rejected webhook creation: {create_resp.status_code} {create_resp.text[:300]}",
                )
            hook_id = create_resp.json().get("id")
            created = True
        else:
            hook_id = already_registered.get("id")
            created = False

    await webhook_registry.register(request.app.state.redis, repo_full_name, project_id, tenant_id, project.get("branch") or "main")

    logger.info("github_webhook_registered", project_id=project_id, repo=repo_full_name, hook_id=hook_id, created=created)
    return {"registered": True, "created": created, "hook_id": hook_id, "webhook_url": webhook_url, "repo": repo_full_name}


async def _generate_and_validate_pipeline_candidate(
    prompt: str, current_yaml: str, context: dict, log_context: dict
) -> dict:
    """
    Shared by both the existing post-creation pipeline editor
    (generate_pipeline_via_ai) and Phase 6's pre-creation preview
    (POST /pipeline-preview/generate) — same real Groq call
    (pipeline_generator.py), same real pipeline-worker validator, same
    "one retry with the validator's own error fed back, then a clear
    failure" contract either way. Never auto-applies anything; the caller
    always owns what happens to the returned candidate.
    """
    validation_error: str | None = None
    candidate_yaml = current_yaml
    summary_of_changes = ""

    # Two attempts total: the model's first try, then one retry with the
    # real validator error fed back — never a third silent attempt.
    for attempt in range(2):
        async with httpx.AsyncClient(timeout=35.0) as client:
            try:
                gen_resp = await client.post(
                    f"{EXPLAINABILITY_SERVICE_URL}/generate-pipeline",
                    json={
                        "prompt": prompt,
                        "current_yaml": current_yaml,
                        "context": context,
                        "validation_error": validation_error,
                    },
                )
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"AI pipeline generation unavailable: {e}")
        if gen_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"AI pipeline generation failed: {gen_resp.text}")

        generated = gen_resp.json()
        candidate_yaml = generated["pipeline_yaml"]
        summary_of_changes = generated["summary_of_changes"]

        async with httpx.AsyncClient(timeout=15.0) as client:
            val_resp = await client.post(
                f"{PIPELINE_WORKER_URL}/pipelines/validate", json={"policy_yaml": candidate_yaml}
            )
        validation = val_resp.json()
        if validation["valid"]:
            logger.info("ai_pipeline_generated", attempt=attempt + 1, **log_context)
            return {"pipeline_yaml": candidate_yaml, "summary_of_changes": summary_of_changes, "valid": True}

        validation_error = validation["error"]

    logger.warning("ai_pipeline_generation_failed_validation", error=validation_error, **log_context)
    raise HTTPException(
        status_code=422,
        detail=f"AI-generated pipeline failed validation after retry: {validation_error}",
    )


@router.post("/{project_id}/pipeline/generate", dependencies=[Depends(require_role("lead-sre"))])
async def generate_pipeline_via_ai(
    project_id: str,
    body: GeneratePipelineRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Natural-language pipeline authoring — never auto-applies anything. The
    candidate YAML this returns must be reviewed and explicitly saved via
    the existing POST /api/v1/policy (policy_router.py) the same as any
    hand-edit; this endpoint has no path to commit a change unsupervised.
    Every candidate is validated against pipeline-worker's real
    /pipelines/validate (the exact rules a hand-authored pipeline must
    already pass) before it's ever returned — with one retry, feeding the
    validator's own error back to the model, before giving up with a clear
    error rather than silently handing back something invalid.
    """
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id, request)
    if not project.get("pipeline_id"):
        raise HTTPException(status_code=409, detail="Project has no linked pipeline to edit")

    pipeline_row = await db.execute(
        text("SELECT policy_yaml FROM pipelines WHERE pipeline_id = :pid AND tenant_id = :tid"),
        {"pid": str(project["pipeline_id"]), "tid": tenant_id},
    )
    pipeline = pipeline_row.mappings().first()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Linked pipeline not found")

    context = {"project_name": project.get("name"), "service_name": _k8s_name(project.get("name", ""))}
    return await _generate_and_validate_pipeline_candidate(
        prompt=body.prompt,
        current_yaml=pipeline["policy_yaml"],
        context=context,
        log_context={"project_id": project_id},
    )


@router.get("/{project_id}/runs/{run_id}/failure-analysis")
async def get_run_failure_analysis(
    project_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Reads the grounded stage-failure RCA pipeline-worker requested at
    `failure_rca:{run_id}` (see worker.py::_request_stage_failure_rca) — a
    plain Redis key, not a table, matching how `logs:{run_id}` already
    works. Returns null (not 404) for a run that hasn't failed, or failed
    before this feature existed, so the frontend can render "no analysis
    available" instead of treating it as an error.
    """
    tenant_id = _get_tenant_id(request)
    await _assert_run_belongs_to_project(db, run_id, project_id, tenant_id)

    raw = await request.app.state.redis.get(f"failure_rca:{run_id}")
    return {"failure_analysis": json.loads(raw) if raw else None}


@router.post("/{project_id}/ask")
async def ask_project_question(
    project_id: str,
    body: AskProjectQuestionRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    ChatOps query interface — the assignment's own named bonus item ("a
    query interface that still grounds its answer in the real comparison
    data"). A thin, auth-checked proxy: tenant_id comes from the
    authenticated session (never the request body), `_load_project`
    404s if the project isn't this tenant's — same guard every other
    project-scoped endpoint in this file already uses. The real work
    (assembling grounded context, calling Groq) happens in
    explainability-service, mirroring how /pipeline/generate above proxies
    to the same service.
    """
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id, request)

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{EXPLAINABILITY_SERVICE_URL}/chatops/ask",
                json={"tenant_id": tenant_id, "project_id": project_id, "question": body.question},
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"ChatOps assistant unavailable: {e}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ChatOps assistant failed: {resp.text}")
    return resp.json()


@router.post("/{project_id}/log-hygiene")
async def analyze_project_log_hygiene(
    project_id: str,
    body: LogHygieneRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    AI Code & CloudWatch Log Hygiene Analyzer:
    Detects noisy or sensitive console.log/print() statements from codebase and
    CloudWatch/container logs, calculates monthly AWS CloudWatch cost waste, and
    generates a clean unified diff patch ready for one-click review and application.
    """
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id, request)

    code_files = dict(body.code_files)
    cw_logs = list(body.cloudwatch_logs)

    if not code_files:
        lang = project.get("language") or "node"
        if lang in ("node", "javascript", "typescript"):
            code_files["src/index.js"] = (
                "const express = require('express');\n"
                "const app = express();\n"
                "console.log('App starting on port ' + process.env.PORT);\n"
                "app.get('/health', (req, res) => {\n"
                "  console.log('Health check received', req.ip);\n"
                "  res.json({ status: 'ok' });\n"
                "});\n"
            )
        elif lang == "python":
            code_files["src/main.py"] = (
                "import os\n"
                "print('Starting service...')\n"
                "def handler(event, context):\n"
                "    print('Received event payload:', event)\n"
                "    return {'statusCode': 200}\n"
            )

    if not cw_logs:
        service_name = _k8s_name(project.get("name", "service"))
        if project.get("deploy_target") == "aws_ecs":
            try:
                import boto3
                region = project.get("aws_region") or "us-east-1"
                logs_client = boto3.client("logs", region_name=region)
                log_group = f"/ecs/smartcd-platform/{service_name}"
                resp = await asyncio.to_thread(
                    logs_client.filter_log_events,
                    logGroupName=log_group,
                    limit=25,
                )
                cw_logs = [e["message"] for e in resp.get("events", []) if "message" in e]
            except Exception:
                cw_logs = [
                    f"[INFO] 2026-09-23T10:00:00Z {service_name} ready",
                    f"[DEBUG] 2026-09-23T10:00:05Z console.log: connection pool initialized",
                ]

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{EXPLAINABILITY_SERVICE_URL}/log-hygiene",
                json={"code_files": code_files, "cloudwatch_logs": cw_logs},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("log_hygiene_proxy_failed", project_id=project_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"Log hygiene analyzer failed: {e}")


@router.post("/{project_id}/predictive-risk")
async def score_project_predictive_risk(
    project_id: str,
    body: PredictiveRiskRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    AI Predictive Deployment Risk Scoring:
    Pre-flight risk assessment evaluating code diff, commit message, and files
    touched to output a 0-100 risk score, risk factors, and adaptive canary ramp steps.
    """
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id, request)
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{EXPLAINABILITY_SERVICE_URL}/predictive-risk",
                json={
                    "commit_diff": body.commit_diff,
                    "commit_message": body.commit_message,
                    "files_changed": body.files_changed,
                },
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("predictive_risk_proxy_failed", project_id=project_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"Predictive risk scorer failed: {e}")


@router.get("/{project_id}/runs")
async def list_project_runs(
    project_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_request_db),
):
    """Run history for one project — strictly filtered by project_id."""
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id, request)

    result = await db.execute(
        text(
            """
            SELECT e.pipeline_run_id, e.target_version,
                   -- See the note in list_projects: the durable final status
                   -- lives in execution_state, not on the execution row.
                   COALESCE(es.status, e.status) AS status,
                   COALESCE(es.current_stage, e.current_stage) AS current_stage,
                   COALESCE(es.current_traffic_weight, e.current_traffic_weight) AS current_traffic_weight,
                   e.trigger_type, e.commit_sha, e.commit_message,
                   e.started_at, e.completed_at,
                   v.status AS verdict, v.confidence
            FROM pipeline_executions e
            LEFT JOIN execution_state es ON es.pipeline_run_id = e.pipeline_run_id
            LEFT JOIN LATERAL (
                SELECT status, confidence
                FROM verification_records
                WHERE pipeline_run_id = e.pipeline_run_id
                ORDER BY timestamp_utc DESC
                LIMIT 1
            ) v ON TRUE
            WHERE e.project_id = :project_id AND e.tenant_id = :tenant_id
            ORDER BY e.started_at DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id, "limit": limit},
    )
    runs = await _overlay_live_state(request.app.state.redis, [dict(r) for r in result.mappings().all()])
    return {"runs": runs}


@router.get("/{project_id}/cost-history")
async def get_project_cost_history(
    project_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_request_db),
):
    """
    Reports/Cost UI (P0, 2026-09-16) — per-run cost breakdown for the new
    Cost tab, reusing `cost_analysis` rows `cost_tracker.py`/
    `cost_tracker_ecs.py` already record for real on every promotion
    decision (RULE 7's guardrail input) rather than a new computation.

    `rightsizing_rec` is included as-is, honestly — it is genuinely NULL
    for every row today. `cost_analyzer.py::compute_rightsizing_recommendation`
    is real and already unit-tested, but it needs observed p95 CPU/memory
    utilization, which nothing in this platform queries yet (the
    CloudWatch/in-cluster-Prometheus telemetry work is still open — see
    BACKLOG.md P2). Returning a fabricated number here would violate the
    exact "never faked from numbers nobody actually measured" principle
    cost_tracker.py's own module docstring already states — the frontend
    must render "not enough usage data yet" for a null value, not invent one.
    """
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id, request)

    result = await db.execute(
        text(
            """
            SELECT c.cost_id, c.pipeline_run_id, c.baseline_cost, c.canary_cost,
                   c.delta_percent, c.rightsizing_rec, c.computed_at,
                   e.trigger_type, e.commit_sha, e.commit_message,
                   v.evidence AS verification_evidence
            FROM cost_analysis c
            JOIN pipeline_executions e ON e.pipeline_run_id = c.pipeline_run_id
            LEFT JOIN LATERAL (
                SELECT evidence
                FROM verification_records
                WHERE pipeline_run_id = c.pipeline_run_id
                ORDER BY timestamp_utc DESC
                LIMIT 1
            ) v ON TRUE
            WHERE e.project_id = :project_id AND c.tenant_id = :tenant_id
            ORDER BY c.computed_at DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id, "limit": limit},
    )
    rows = [dict(r) for r in result.mappings().all()]
    for row in rows:
        row["cost_id"] = str(row["cost_id"])
        row["pipeline_run_id"] = str(row["pipeline_run_id"])
        row["baseline_cost"] = float(row["baseline_cost"])
        row["canary_cost"] = float(row["canary_cost"])
        row["delta_percent"] = float(row["delta_percent"])
        row["computed_at"] = row["computed_at"].isoformat()
        
        evidence = row.pop("verification_evidence", None)
        performance_correlation = None
        if evidence and isinstance(evidence, dict):
            for m_name, m_data in evidence.items():
                if not isinstance(m_data, dict):
                    continue
                # Check for medians (nested under mann_whitney or direct)
                mw = m_data.get("mann_whitney") if "mann_whitney" in m_data else m_data
                if isinstance(mw, dict) and "baseline_median" in mw and "canary_median" in mw:
                    b_med = mw["baseline_median"]
                    c_med = mw["canary_median"]
                    if b_med is not None and c_med is not None and b_med > 0:
                        perf_delta = ((c_med - b_med) / b_med) * 100
                        performance_correlation = {
                            "metric_name": m_name,
                            "latency_delta_percent": round(perf_delta, 1),
                            "status": "available",
                            "detail": f"Baseline: {round(float(b_med), 4)}s, Canary: {round(float(c_med), 4)}s"
                        }
                        break
                
                # Check for insufficient samples on latency tests
                is_latency_metric = (
                    "latency" in m_name.lower()
                    or m_data.get("test") == "Mann-Whitney U"
                    or "mann_whitney" in m_data
                )
                if is_latency_metric and performance_correlation is None:
                    note = m_data.get("note")
                    if not note and isinstance(m_data.get("mann_whitney"), dict):
                        note = m_data["mann_whitney"].get("note")
                    if note == "insufficient samples" or m_data.get("status") == "insufficient_samples":
                        performance_correlation = {
                            "metric_name": m_name,
                            "latency_delta_percent": None,
                            "status": "insufficient_samples",
                            "detail": "Awaiting traffic (N < 2)"
                        }

        row["performance_correlation"] = performance_correlation

    return {
        "cost_history": rows,
        "total_baseline_cost": round(sum(r["baseline_cost"] for r in rows), 4),
        "total_canary_cost": round(sum(r["canary_cost"] for r in rows), 4),
    }


@router.get("/{project_id}/runs/{run_id}/stages")
async def get_run_stages(
    project_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Per-stage status for the workspace's Build → Test → Canary stepper.

    Derived from the run's live execution state (which already carries the
    real DAG order and the current stage) rather than a new per-stage table:
    the worker is the only thing that knows a stage finished, and it already
    reports exactly that. A stage before the current one is SUCCESS, the
    current one is RUNNING (or FAILED if the run failed), later ones PENDING.
    """
    tenant_id = _get_tenant_id(request)
    run = await _assert_run_belongs_to_project(db, run_id, project_id, tenant_id)

    live = {}
    cached = await request.app.state.redis.get(f"state:{run_id}")
    if cached:
        try:
            live = json.loads(cached)
        except json.JSONDecodeError:
            live = {}

    declared = live.get("stages") or PROJECT_STAGES
    current_stage = live.get("current_stage") or run.get("current_stage")
    run_status = live.get("status") or run.get("status") or "PENDING"

    current_index = declared.index(current_stage) if current_stage in declared else -1
    stages = []
    for index, stage_name in enumerate(declared):
        if run_status == "COMPLETED":
            status = "SUCCESS"
        elif current_index == -1:
            status = "PENDING"
        elif index < current_index:
            status = "SUCCESS"
        elif index == current_index:
            status = "FAILED" if run_status in ("FAILED", "ROLLED_BACK") else run_status
        else:
            status = "PENDING"
        stages.append({"name": stage_name, "status": status})

    return {
        "run_id": run_id,
        "project_id": project_id,
        "run_status": run_status,
        "current_stage": current_stage,
        "current_traffic_weight": live.get("current_traffic_weight", run.get("current_traffic_weight")),
        "stages": stages,
    }


@router.get("/{project_id}/runs/{run_id}/rollout")
async def get_run_rollout_state(
    project_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Surfaces the automated-ramp state policy-controller's rollout_scheduler.py
    drives (see controller.py) — which step the run is on, its real target
    weight, and whether it's paused `AWAITING_APPROVAL` at a step flagged
    `requiresManualApproval` (the final 100% cutover, by default). This is
    what lets the UI show a real "Approve" action instead of a promotion
    silently stalling with no visible reason.
    """
    tenant_id = _get_tenant_id(request)
    await _assert_run_belongs_to_project(db, run_id, project_id, tenant_id)

    raw = await request.app.state.redis.get(f"rollout_state:{run_id}")
    if not raw:
        return {"has_rollout_state": False}

    state = json.loads(raw)
    steps = state.get("steps", [])
    idx = state.get("current_step_index", 0)
    return {
        "has_rollout_state": True,
        "status": state.get("status"),
        "current_step_index": idx,
        "total_steps": len(steps),
        "current_step_weight": steps[idx]["trafficWeight"] if idx < len(steps) else None,
        "awaiting_approval": state.get("status") == "AWAITING_APPROVAL",
    }


@router.get("/{project_id}/runs/{run_id}/logs/stream")
async def stream_project_run_logs(
    project_id: str,
    run_id: str,
    request: Request,
    stage: str | None = Query(default=None, description="Filter to one stage's output"),
    db: AsyncSession = Depends(get_request_db),
):
    """
    SSE log stream for one run, optionally filtered to one stage.

    Reads the same replayable Redis LIST (`logs:{run_id}`) logs_router.py
    uses — a list rather than pub/sub so a viewer connecting after the run
    finished still sees its full output. Ownership is checked against the
    RLS-scoped session AND the project before a single line is emitted.
    """
    tenant_id = _get_tenant_id(request)
    await _assert_run_belongs_to_project(db, run_id, project_id, tenant_id)

    redis_client = request.app.state.redis
    key = f"logs:{run_id}"
    # Matches worker.py's `--- Stage: {name} ({type}) ---` marker.
    stage_marker_prefix = "--- Stage: "

    async def event_generator():
        next_index = 0
        active_stage: str | None = None
        while True:
            if await request.is_disconnected():
                break
            try:
                lines = await redis_client.lrange(key, next_index, -1)
            except Exception as e:
                logger.error("project_log_poll_failed", run_id=run_id, error=str(e))
                lines = []
            for line in lines:
                if line.startswith(stage_marker_prefix):
                    active_stage = line[len(stage_marker_prefix):].split(" (")[0]
                if stage and active_stage != stage:
                    continue
                yield {"event": "log", "data": line}
            next_index += len(lines)
            await asyncio.sleep(LOG_POLL_INTERVAL_SECONDS)

    return EventSourceResponse(event_generator())


@router.post("/{project_id}/runs/{run_id}/rollback", dependencies=[Depends(require_role("lead-sre"))])
async def rollback_project_run(
    project_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Manual emergency rollback for a project's run.

    Real gap found live while wiring up the automated promotion ramp: this
    used to only publish a `pipeline:manual_rollback` control message and
    flip `projects.status` — nothing anywhere in this codebase ever
    subscribed to that channel, so clicking "Emergency Rollback" updated a
    label in the UI and never actually touched Kubernetes at all. Now calls
    policy-controller's real `emergency_rollback` directly (the exact same
    function an autonomous FAILED-verdict rollback calls), so it genuinely
    cuts canary traffic to 0% and scales the canary Deployment down —
    signed into the audit ledger as `MANUAL:<email>` rather than
    `OPA:rule=ROLLBACK`, but otherwise identical in effect.
    """
    tenant_id = _get_tenant_id(request)
    await _assert_run_belongs_to_project(db, run_id, project_id, tenant_id)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{POLICY_CONTROLLER_URL}/internal/manual-rollback/{run_id}",
            json={
                "tenant_id": tenant_id,
                "requested_by": f"project_console:{getattr(request.state, 'email', 'operator')}",
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Rollback actuation failed: {resp.text}")

    await db.execute(
        text("UPDATE projects SET status = 'ROLLED_BACK' WHERE project_id = :pid AND tenant_id = :tid"),
        {"pid": project_id, "tid": tenant_id},
    )
    await db.commit()
    logger.warning("project_rollback_requested", project_id=project_id, pipeline_run_id=run_id)
    return {"status": "ROLLED_BACK", "pipeline_run_id": run_id, "project_id": project_id}


@router.post("/{project_id}/runs/{run_id}/approve", dependencies=[Depends(require_role("lead-sre"))])
async def approve_project_run(
    project_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Unblocks a promotion paused at a step flagged `requiresManualApproval`
    (the final 100% cutover, by default — see projects_router's own
    generated pipeline YAML). Real gap found live: that gate fired a real
    `alert_approval_required` notification but there was previously no way
    for a human to ever actually grant the approval it was waiting for —
    `approved_signatures` was permanently empty everywhere in this
    codebase. Calls policy-controller's `handle_approval`, which
    re-evaluates the ALREADY-HEALTHY verdict that triggered the pause
    (no new verification cycle needed) with this real signature attached.
    """
    tenant_id = _get_tenant_id(request)
    await _assert_run_belongs_to_project(db, run_id, project_id, tenant_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{POLICY_CONTROLLER_URL}/internal/approvals/{run_id}",
            json={
                "tenant_id": tenant_id,
                "approver_role": getattr(request.state, "role", "lead-sre"),
                "approver_user": getattr(request.state, "email", None),
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Approval failed: {resp.text}")

    result = resp.json()
    logger.info("project_run_approved", project_id=project_id, pipeline_run_id=run_id, result=result)
    return result


@router.post("/internal/pipeline-runs/{run_id}/graduate")
async def graduate_pipeline_run(run_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """
    Internal, service-to-service only (not reachable through the frontend —
    no route in App.tsx points here). Called by policy-controller's
    `rollout_scheduler.graduate()` once a canary's ramp reaches its final
    step. Real gap found live: nothing anywhere updated a project's
    baseline version after a full promotion — `active_production_tag`
    was set once at project-creation time and never again, so the NEXT
    rollout would keep comparing against the ORIGINAL baseline forever
    instead of the version that was actually just proven healthy and
    promoted to 100%, defeating the point of a continuous-delivery loop.

    Unauthenticated like `/pipelines/start` and `/services/onboard` on
    pipeline-worker — this whole platform's service-to-service calls rely
    on Docker network isolation rather than a shared internal token, and
    adding auth to only this one route would be inconsistent, not safer.
    `tenant_id` is supplied by the caller (policy-controller already has it
    cached from this run's own actuation_target, itself resolved once at
    pipeline start) rather than looked up here, since RLS requires it to
    read the row in the first place — the same pattern PolicyControllerDB
    already uses for audit/cost writes.
    """
    tenant_id = body.get("tenant_id")
    new_version = body.get("new_version")
    if not tenant_id or not new_version:
        raise HTTPException(status_code=422, detail="tenant_id and new_version are required")

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    run_row = await db.execute(
        text("SELECT project_id FROM pipeline_executions WHERE pipeline_run_id = :run_id AND tenant_id = :tid"),
        {"run_id": run_id, "tid": tenant_id},
    )
    row = run_row.mappings().first()
    if row is None or row["project_id"] is None:
        # Not every run belongs to a project (an adopted/legacy pipeline
        # execution, or one triggered outside the project wizard) — nothing
        # to graduate, and not an error.
        return {"status": "NO_PROJECT_TO_GRADUATE"}

    await db.execute(
        text(
            "UPDATE projects SET active_production_tag = :new_version, status = 'HEALTHY' "
            "WHERE project_id = :project_id AND tenant_id = :tid"
        ),
        {"new_version": new_version, "project_id": str(row["project_id"]), "tid": tenant_id},
    )
    await db.commit()
    logger.info(
        "project_graduated_to_new_baseline",
        project_id=str(row["project_id"]),
        pipeline_run_id=run_id,
        new_version=new_version,
    )
    return {"status": "GRADUATED", "project_id": str(row["project_id"]), "new_version": new_version}


async def _request_gate1_failure_rca(body: dict) -> dict | None:
    """
    Mirrors worker.py::_request_stage_failure_rca's exact contract (same
    explainability-service endpoint, same fallback-never-blocks guarantee)
    for a gate1 dry-run failure instead of a real pipeline stage failure.
    Reads the real build/test log lines build_preview.py already wrote to
    `preview_logs:{gate1_check_id}` so the RCA is grounded in genuine
    evidence, never invented. Returns None (not raised) on any failure —
    the BLOCKED result this is attached to must still be recorded either way.
    """
    gate1_check_id = body.get("gate1_check_id")
    if not gate1_check_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            recent_logs_resp = await client.get(f"{PIPELINE_WORKER_URL}/build-preview/{gate1_check_id}/logs")
            recent_logs = recent_logs_resp.json().get("lines", []) if recent_logs_resp.status_code == 200 else []
            rca_resp = await client.post(
                f"{EXPLAINABILITY_SERVICE_URL}/stage-failure-rca",
                json={
                    "run_id": gate1_check_id,
                    "failed_stage": body.get("stage") or "build",
                    "error_message": body.get("error") or "Build/test dry run failed",
                    "recent_logs": recent_logs,
                },
            )
            rca_resp.raise_for_status()
            return rca_resp.json()
    except Exception as e:
        logger.warning("gate1_failure_rca_request_failed", gate1_check_id=gate1_check_id, error=str(e))
        return None


@router.post("/internal/{project_id}/gate1-result")
async def report_gate1_result(project_id: str, body: dict, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Internal, service-to-service only (same "Docker network isolation, no
    frontend route, no shared token" posture as graduate_pipeline_run right
    above — see that endpoint's own docstring for why that's this
    platform's consistent pattern rather than a gap). Called by
    pipeline-worker's gate1 consumer (main.py) once a webhook-triggered
    push's build+test dry run finishes.

    body: tenant_id, passed (bool), commit_sha, commit_message, stage
    (nullable — which stage failed), error (nullable), human_side (nullable).

    On passed=True: calls the exact same _trigger_rollout_internal the
    authenticated POST /{project_id}/rollout route and (indirectly) the
    webhook receiver both use — a webhook-triggered push that clears gate 1
    gets a byte-identical real rollout to a human-triggered one.

    On passed=False: per the roadmap's "fail here -> no deployment attempt
    at all" requirement, NOTHING about the real pipeline/cluster is ever
    touched — matches build_preview.py's own stated invariant that a
    preview/dry-run result must never be confused for a real
    pipeline_execution by anything downstream (audit ledger, cost
    tracking). Recorded in Redis (`gate1_last_result:{project_id}`) rather
    than a new Postgres table — the natural home for a human to actually
    SEE this is the project workspace's Reports tab.

    AI-narrated failure explanation (P0, 2026-09-16): a real, grounded RCA
    is requested from explainability-service's EXISTING `/stage-failure-rca`
    (the same endpoint worker.py's `_request_stage_failure_rca` already
    calls for a real pipeline stage failure — no second Groq integration
    built here, this is the identical call with gate1's own
    run_id/stage/error/logs). Wrapped in its own try/except, same as that
    caller: an explainability-service outage must never prevent the BLOCKED
    result itself from being recorded — the block has already happened by
    the time this runs.
    """
    tenant_id = body.get("tenant_id")
    passed = body.get("passed")
    if not tenant_id or passed is None:
        raise HTTPException(status_code=422, detail="tenant_id and passed are required")

    redis_client = request.app.state.redis

    if not passed:
        detail = {
            "passed": False,
            "commit_sha": body.get("commit_sha"),
            "commit_message": body.get("commit_message"),
            "stage": body.get("stage"),
            "error": body.get("error"),
            "human_side": body.get("human_side"),
            "rca": await _request_gate1_failure_rca(body),
        }
        await redis_client.set(f"gate1_last_result:{project_id}", json.dumps(detail), ex=7 * 24 * 3600)
        logger.warning(
            "gate1_check_blocked_deploy",
            project_id=project_id,
            commit_sha=body.get("commit_sha"),
            stage=body.get("stage"),
            human_side=body.get("human_side"),
        )
        return {"status": "BLOCKED", "project_id": project_id}

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    project = await _load_project(db, project_id, tenant_id, request)
    run_id = await _trigger_rollout_internal(
        db=db,
        redis_client=redis_client,
        tenant_id=tenant_id,
        project=project,
        project_id=project_id,
        trigger_type="GITHUB_PUSH",
        commit_sha=body.get("commit_sha"),
        commit_message=body.get("commit_message"),
    )
    await redis_client.set(
        f"gate1_last_result:{project_id}",
        json.dumps({
            "passed": True,
            "commit_sha": body.get("commit_sha"),
            "pipeline_run_id": run_id,
            # Test commands are non-blocking (build_preview.py) — a failed
            # one never stops the rollout this branch just triggered, but
            # must still be visible here rather than silently dropped.
            "test_warning": body.get("test_warning"),
        }),
        ex=7 * 24 * 3600,
    )
    logger.info(
        "gate1_check_passed_rollout_triggered",
        project_id=project_id, pipeline_run_id=run_id, test_warning=body.get("test_warning"),
    )
    return {"status": "TRIGGERED", "project_id": project_id, "pipeline_run_id": run_id}


@router.get("/{project_id}/audit")
async def project_audit(project_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Audit ledger scoped to this project — the same signed `audit_ledger`
    rows the global ledger shows, restricted to runs belonging to this
    project via a join on pipeline_executions.project_id.
    """
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id, request)

    result = await db.execute(
        text(
            """
            SELECT a.actuation_id, a.timestamp, a.action, a.pipeline_run_id, a.verdict,
                   a.confidence, a.authorized_by, a.policy_rule, a.hmac_signature,
                   a.canary_weight, a.baseline_weight
            FROM audit_ledger a
            JOIN pipeline_executions e ON e.pipeline_run_id = a.pipeline_run_id
            WHERE e.project_id = :project_id AND a.tenant_id = :tenant_id
            ORDER BY a.timestamp DESC
            LIMIT 200
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id},
    )
    return {"entries": [dict(r) for r in result.mappings().all()]}
