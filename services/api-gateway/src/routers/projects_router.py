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
from src.auth.rbac import require_role
from src.db.session import get_db, get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

STREAM_PIPELINE_START = "stream:pipeline:start"
PIPELINE_WORKER_URL = os.environ.get("PIPELINE_WORKER_URL", "http://pipeline-worker:8001")
POLICY_CONTROLLER_URL = os.environ.get("POLICY_CONTROLLER_URL", "http://policy-controller:8003")
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
    dockerfile_path: str = "Dockerfile"
    test_command: str | None = None
    container_image: str
    active_production_tag: str = "v1.0.0"
    canary_tag: str | None = "v1.1.0"
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
    traffic_steps: list[int] = Field(default_factory=lambda: [10, 25, 50, 100])
    # Best-effort: generate + apply the real Deployment/Service/HTTPRoute via
    # pipeline-worker. Requires a reachable cluster; when it fails the project
    # is still created and the failure is reported (see create_project).
    provision_cluster: bool = True


class TriggerRolloutRequest(BaseModel):
    commit_sha: str | None = None
    commit_message: str | None = None
    target_version: str | None = None
    trigger_type: str = "MANUAL_UI"


# ─────────────────────────── helpers ───────────────────────────


def _get_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


def _metric_prefix(project_name: str) -> str:
    return project_name.replace("-", "_").replace(".", "_")


def generate_project_pipeline_yaml(body: CreateProjectRequest, tenant_id: str, namespace: str) -> str:
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
    # minDuration/minSampleSize scale with the traffic step: a 10% canary is
    # allowed a shorter, smaller-sample window than a 100% cutover. The final
    # step requires manual approval, matching the generated onboarding
    # pipeline's own gate (manifest_generator.generate_pipeline_yaml).
    step_lines = []
    for weight in steps:
        is_final = weight >= 100
        duration = 0 if is_final else max(120, weight * 6)
        sample = 0 if is_final else max(body.guardrails.min_sample_size, weight * 10)
        step_lines.append(f"          - trafficWeight: {weight}")
        step_lines.append(f"            minDuration: {duration}s")
        step_lines.append(f"            minSampleSize: {sample}")
        if is_final:
            step_lines.append("            requiresManualApproval: true")

    prefix = _metric_prefix(body.name)

    build_and_test_stages = ""
    if body.source_type != "existing_image":
        dockerfile_in_repo = os.path.join(
            body.root_directory.strip("./") or "", body.dockerfile_path
        ).replace("\\", "/").lstrip("/")
        test_command = body.test_command or "echo 'no test command configured'"
        canary_tag = body.canary_tag or "v1.1.0"
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
        build_and_test_stages = f"""    - name: build
      type: build
      config:
        repoUrl: {body.repo_url}
        repoRef: {body.branch}{credentials_line}
        dockerfilePath: {dockerfile_in_repo or 'Dockerfile'}
        imageTag: {canary_tag}
        image: {body.container_image}{registry_credential_line}

    - name: test
      type: test
      config:
        command: {test_command}

"""

    return f"""apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: {body.name}-rollout
  tenantId: "{tenant_id}"
  namespace: {namespace}

spec:
  stages:
{build_and_test_stages}    - name: canary_verify
      type: canary_loop
      config:
        gatewayRef: local-edge-gateway
        service: {body.name}
        routeName: {body.name}-route
        canaryDeployment: {body.name}-canary
        steps:
{chr(10).join(step_lines)}

  gates:
    blockedDeployWindows: []
    manualApprovalRequired:
      beforeStages: [step_100_promotion]
      approverRoles: ["lead-sre", "platform-admin"]

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
        p1: 0.020
      - name: {prefix}_p95_latency_seconds
        category: latency
        tier: important
        alpha: 0.05
        weight: 2.5
"""


async def _load_project(db: AsyncSession, project_id: str, tenant_id: str) -> dict:
    result = await db.execute(
        text(
            """
            SELECT project_id, tenant_id, pipeline_id, name, repo_url, branch, root_directory,
                   dockerfile_path, test_command, container_image, active_production_tag,
                   canary_tag, status, created_at
            FROM projects
            WHERE project_id = :project_id AND tenant_id = :tenant_id
            """
        ),
        {"project_id": project_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


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
                   p.status, p.created_at,
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

    tenant_id = _get_tenant_id(request)
    namespace = f"tenant-{tenant_id.split('-')[0]}"
    policy_yaml = generate_project_pipeline_yaml(body, tenant_id, namespace)

    # Fail fast on a manifest this platform's own loader would reject, rather
    # than storing YAML that only explodes later inside the worker.
    try:
        parsed = yaml.safe_load(policy_yaml)
        if not parsed or "spec" not in parsed:
            raise ValueError("generated manifest has no 'spec' section")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generated pipeline YAML is invalid: {e}")

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
                    dockerfile_path, test_command, container_image, active_production_tag,
                    canary_tag, status
                ) VALUES (
                    :project_id, :tenant_id, :pipeline_id, :name, :repo_url, :branch, :root_directory,
                    :dockerfile_path, :test_command, :container_image, :active_production_tag,
                    :canary_tag, 'IDLE'
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
                "test_command": body.test_command,
                "container_image": body.container_image,
                "active_production_tag": body.active_production_tag,
                "canary_tag": body.canary_tag,
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A project named '{body.name}' already exists for this tenant.",
        )

    provisioning: dict = {"attempted": body.provision_cluster, "succeeded": False, "detail": None}
    if body.provision_cluster:
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                resp = await http_client.post(
                    f"{PIPELINE_WORKER_URL}/services/onboard",
                    json={
                        "service_name": body.name,
                        "image": body.container_image,
                        "baseline_tag": body.active_production_tag,
                        "canary_tag": body.canary_tag or "v1.1.0",
                        "port": body.port,
                        "health_check_path": body.health_check_path,
                        "path_prefix": body.path_prefix or f"/api/v1/{body.name}",
                        "tenant_id": tenant_id,
                        "namespace": namespace,
                        "registry_credential_id": body.registry_credential_id,
                    },
                )
            provisioning["succeeded"] = resp.status_code == 200
            if resp.status_code != 200:
                provisioning["detail"] = f"pipeline-worker returned {resp.status_code}: {resp.text[:300]}"
        except httpx.RequestError as e:
            provisioning["detail"] = f"pipeline-worker unreachable: {e}"

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
    }


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id)

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
    """
    tenant_id = _get_tenant_id(request)
    project = await _load_project(db, project_id, tenant_id)

    await db.execute(
        text("DELETE FROM projects WHERE project_id = :project_id AND tenant_id = :tenant_id"),
        {"project_id": project_id, "tenant_id": tenant_id},
    )
    if project.get("pipeline_id"):
        # Best-effort: a pipeline that somehow has other executions attached
        # (a run triggered directly against it before it was linked) must not
        # take this DELETE down with a FK violation.
        try:
            await db.execute(
                text("DELETE FROM pipelines WHERE pipeline_id = :pid AND tenant_id = :tid"),
                {"pid": str(project["pipeline_id"]), "tid": tenant_id},
            )
        except IntegrityError:
            await db.rollback()
            logger.warning("project_pipeline_retained", project_id=project_id)
            return {"deleted": project_id, "pipeline_retained": True}
    await db.commit()
    logger.info("project_deleted", project_id=project_id)
    return {"deleted": project_id, "pipeline_retained": False}


# ─────────────────────────── execution ───────────────────────────


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
    project = await _load_project(db, project_id, tenant_id)
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
    target_version = body.target_version or project.get("canary_tag") or "v1.1.0"
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
            "target_version": target_version,
            "trigger_type": body.trigger_type,
            "commit_sha": body.commit_sha,
            "commit_message": body.commit_message,
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
    # deletes the key itself right after the clone either way.
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        gh_token = await request.app.state.redis.get(f"github:token:{user_id}")
        if gh_token:
            await request.app.state.redis.set(f"clone_token:{run_id}", gh_token, ex=900)

    await streams.publish(
        request.app.state.redis,
        STREAM_PIPELINE_START,
        {
            "pipeline_run_id": run_id,
            "pipeline_id": str(project["pipeline_id"]),
            "tenant_id": tenant_id,
            "target_version": target_version,
            "trace_id": getattr(request.state, "trace_id", None),
            "policy_yaml": pipeline["policy_yaml"],
        },
    )
    logger.info(
        "project_rollout_triggered",
        project_id=project_id,
        pipeline_run_id=run_id,
        trigger_type=body.trigger_type,
    )
    return {"pipeline_run_id": run_id, "project_id": project_id, "status": "PENDING"}


@router.get("/{project_id}/runs")
async def list_project_runs(
    project_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_request_db),
):
    """Run history for one project — strictly filtered by project_id."""
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id)

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


@router.get("/{project_id}/audit")
async def project_audit(project_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Audit ledger scoped to this project — the same signed `audit_ledger`
    rows the global ledger shows, restricted to runs belonging to this
    project via a join on pipeline_executions.project_id.
    """
    tenant_id = _get_tenant_id(request)
    await _load_project(db, project_id, tenant_id)

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
