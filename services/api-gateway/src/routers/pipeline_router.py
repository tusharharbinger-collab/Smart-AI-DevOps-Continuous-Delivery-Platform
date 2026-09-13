"""
services/api-gateway/src/routers/pipeline_router.py

Pipeline registration + run triggering. Spec §4, §11.1 (Screen 1: Pipeline View).

Registering a pipeline persists its declarative YAML (§4.1) to Postgres.
Triggering a run creates a `pipeline_executions` row and enqueues a
`pipeline:start` command onto a Redis Stream (Phase 5 — see
shared/redis_streams.py), which exactly one pipeline-worker replica's
consumer-group reader picks up to build the DAG (§4.2) and begin executing
stages against Kind. This used to be a bare pub/sub `publish()`, which
every subscriber receives — with 2+ pipeline-worker replicas that meant
every triggered run was processed once PER replica.
"""
import uuid
from datetime import datetime, timezone

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared import redis_streams as streams
from src.db.session import get_request_db

STREAM_PIPELINE_START = "stream:pipeline:start"

router = APIRouter()
logger = structlog.get_logger(__name__)


class RegisterPipelineRequest(BaseModel):
    name: str
    policy_yaml: str


class TriggerRunRequest(BaseModel):
    target_version: str


def _get_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


@router.post("")
async def register_pipeline(
    body: RegisterPipelineRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """Registers a new pipeline from its declarative YAML (validated, not executed)."""
    try:
        parsed = yaml.safe_load(body.policy_yaml)
        if not parsed or "spec" not in parsed:
            raise ValueError("Pipeline YAML missing required 'spec' section")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid pipeline YAML: {e}")

    tenant_id = _get_tenant_id(request)
    pipeline_id = str(uuid.uuid4())
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
            "name": body.name,
            "policy_yaml": body.policy_yaml,
        },
    )
    await db.commit()
    logger.info("pipeline_registered", pipeline_id=pipeline_id, name=body.name)
    return {"pipeline_id": pipeline_id, "name": body.name}


@router.get("")
async def list_pipelines(request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = _get_tenant_id(request)
    result = await db.execute(
        text(
            "SELECT pipeline_id, name, created_at FROM pipelines "
            "WHERE tenant_id = :tenant_id ORDER BY created_at DESC"
        ),
        {"tenant_id": tenant_id},
    )
    rows = result.mappings().all()
    return {"pipelines": [dict(r) for r in rows]}


@router.post("/{pipeline_id}/runs")
async def trigger_run(
    pipeline_id: str,
    body: TriggerRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Creates a pipeline_executions row and publishes a start command to Redis
    for the pipeline-worker to pick up. This is what backs the "Run" button
    on Screen 1 (Pipeline View).
    """
    tenant_id = _get_tenant_id(request)
    row = await db.execute(
        text("SELECT policy_yaml FROM pipelines WHERE pipeline_id = :pid AND tenant_id = :tid"),
        {"pid": pipeline_id, "tid": tenant_id},
    )
    pipeline_row = row.mappings().first()
    if pipeline_row is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            """
            INSERT INTO pipeline_executions
                (pipeline_run_id, tenant_id, pipeline_id, target_version, status, started_at)
            VALUES
                (:run_id, :tenant_id, :pipeline_id, :target_version, 'PENDING', :started_at)
            """
        ),
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "pipeline_id": pipeline_id,
            "target_version": body.target_version,
            "started_at": now,
        },
    )
    await db.commit()

    redis_client = request.app.state.redis
    await streams.publish(
        redis_client,
        STREAM_PIPELINE_START,
        {
            "pipeline_run_id": run_id,
            "pipeline_id": pipeline_id,
            "tenant_id": tenant_id,
            "target_version": body.target_version,
            # Phase 6 (§06-observability-platform-ops.md, 6.1): the same
            # trace_id auth/middleware.py already binds for this HTTP
            # request travels with the queued command so every downstream
            # service's logs for this run carry it too — the whole point
            # being "pull every log line for one pipeline_run_id across all
            # 4+ services in one query," which needs a value that's present
            # from the very first log line, not invented separately by each
            # service that happens to touch this run.
            "trace_id": getattr(request.state, "trace_id", None),
            # Full policy YAML travels with the command so pipeline-worker's
            # consumer (services/pipeline-worker/src/main.py) can write it to
            # a manifest file and hand it to PipelineOrchestrator.start_pipeline
            # without needing its own Postgres round-trip.
            "policy_yaml": pipeline_row["policy_yaml"],
        },
    )
    logger.info("pipeline_run_triggered", pipeline_run_id=run_id, pipeline_id=pipeline_id)
    return {"pipeline_run_id": run_id, "status": "PENDING"}


@router.get("/{pipeline_id}/runs")
async def list_runs(pipeline_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    pipeline-worker never opens a Postgres connection — it only ever writes
    live state to Redis (`state:{run_id}`). The `pipeline_executions.status`
    column is written exactly once, as 'PENDING', at trigger time, and never
    updated after that — so every row would otherwise show PENDING forever
    regardless of what actually happened. Overlay each row with its live
    Redis state where one exists, the same way get_run() already does for a
    single run, so the list reflects reality instead of the insert-time snapshot.
    """
    import json

    tenant_id = _get_tenant_id(request)
    result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, target_version, status, current_stage,
                   current_traffic_weight, started_at, completed_at
            FROM pipeline_executions
            WHERE pipeline_id = :pipeline_id AND tenant_id = :tenant_id
            ORDER BY started_at DESC
            """
        ),
        {"pipeline_id": pipeline_id, "tenant_id": tenant_id},
    )
    rows = [dict(r) for r in result.mappings().all()]

    redis_client = request.app.state.redis
    for row in rows:
        cached = await redis_client.get(f"state:{row['pipeline_run_id']}")
        if cached:
            live = json.loads(cached)
            row["status"] = live.get("status", row["status"])
            row["current_stage"] = live.get("current_stage", row["current_stage"])
            row["current_traffic_weight"] = live.get("current_traffic_weight", row["current_traffic_weight"])

    return {"runs": rows}


@router.get("/runs/{pipeline_run_id}")
async def get_run(pipeline_run_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """
    Live status for Screen 1: prefers the fast Redis path (`state:{run_id}`,
    written by pipeline-worker's ExecutionStateStore) and falls back to the
    durable Postgres row if Redis has no entry.
    """
    tenant_id = _get_tenant_id(request)
    redis_client = request.app.state.redis
    cached = await redis_client.get(f"state:{pipeline_run_id}")
    if cached:
        import json

        live = json.loads(cached)
        # Real bug found live (adversarial cross-tenant test): this Redis
        # fast path has no RLS equivalent — Redis doesn't know about
        # tenants, so unlike the Postgres fallback below, nothing stopped
        # tenant B from reading tenant A's live run just by guessing/knowing
        # its run_id. worker.py's PipelineExecutionState already carries
        # tenant_id, so check it here instead of adding a DB round-trip.
        if live.get("tenant_id") != tenant_id:
            raise HTTPException(status_code=404, detail="Pipeline run not found")
        return live

    result = await db.execute(
        text(
            """
            SELECT pipeline_run_id, target_version, status, current_stage,
                   current_traffic_weight, started_at, completed_at
            FROM pipeline_executions
            WHERE pipeline_run_id = :run_id AND tenant_id = :tenant_id
            """
        ),
        {"run_id": pipeline_run_id, "tenant_id": tenant_id},
    )
    run = result.mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return dict(run)
