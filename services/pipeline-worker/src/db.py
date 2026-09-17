"""
services/pipeline-worker/src/db.py

Phase 5 (§05-reliability-scale.md, deliverable 5.2) — pipeline-worker's
first real Postgres connection. Before this, `ExecutionStateStore.save_state`
had a Postgres write path that was never actually reachable (`db_session`
was always `None` in the real running app — `PipelineOrchestrator` is
constructed with only a Redis client in `main.py`), so `execution_state`
rows were never durably persisted and `reconciler.py`'s "resume an
interrupted pipeline" logic could never find anything to resume.

Two separate connection roles, deliberately:
  - `save_execution_state` writes through APP_POSTGRES_DSN (the RLS-enforced
    `app_user` role) with `app.active_tenant_id` set to THAT RUN's own
    tenant_id for the duration of the write — the same per-request RLS
    discipline api-gateway's middleware uses, just scoped to one write
    instead of one HTTP request.
  - `get_interrupted_pipelines` deliberately uses the superuser POSTGRES_DSN
    (bypasses RLS) because a startup reconciliation sweep is inherently
    cross-tenant by design: a crashed worker could have interrupted
    pipelines belonging to ANY tenant, and there is no single tenant
    context to scope it to. This mirrors `users`/`auth_events`
    (db/schema.sql) having no RLS policy for the same reason — a system
    operation that needs visibility across/before tenant context, not a
    per-tenant user-facing query. Every other pipeline-worker code path
    stays on the RLS-scoped connection.
"""
import asyncio
import json
import os
from datetime import datetime

import asyncpg
import structlog

logger = structlog.get_logger(__name__)


def _to_asyncpg_dsn(sqlalchemy_style_dsn: str) -> str:
    return sqlalchemy_style_dsn.replace("postgresql+asyncpg://", "postgresql://")


APP_DSN = _to_asyncpg_dsn(
    os.environ.get("APP_POSTGRES_DSN", "postgresql://app_user:app_password@localhost:5432/platform")
)
SUPERUSER_DSN = _to_asyncpg_dsn(
    os.environ.get("POSTGRES_DSN", "postgresql://platform:platform@localhost:5432/platform")
)


class PipelineWorkerDB:
    def __init__(self):
        self._app_pool: asyncpg.Pool | None = None
        self._superuser_pool: asyncpg.Pool | None = None
        # `PipelineOrchestrator.start_pipeline` (worker.py) is a plain sync
        # method run via `asyncio.to_thread` — from inside that thread,
        # `asyncio.run(coro)` would spin up a BRAND NEW event loop, but this
        # pool's connections are bound to the loop `connect()` ran on. Using
        # them from a different loop is a real asyncpg failure mode
        # (`ConnectionDoesNotExistError: connection was closed in the middle
        # of operation` — hit this live before adding `loop`/`run_from_thread`
        # below). `run_coroutine_threadsafe` instead schedules the coroutine
        # back onto the loop the pool actually belongs to.
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        self._app_pool = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=5)
        self._superuser_pool = await asyncpg.create_pool(SUPERUSER_DSN, min_size=1, max_size=2)
        logger.info("pipeline_worker_db_connected")

    def run_from_thread(self, coro, timeout: float = 10.0):
        """
        Call from a non-event-loop thread (e.g. inside `start_pipeline`,
        which runs via `asyncio.to_thread`) to run a coroutine that touches
        this pool, on the event loop it actually belongs to, and block the
        calling thread for the result.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    async def close(self):
        if self._app_pool:
            await self._app_pool.close()
        if self._superuser_pool:
            await self._superuser_pool.close()

    async def save_execution_state(self, state) -> None:
        async with self._app_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", state.tenant_id)
                await conn.execute(
                    """
                    INSERT INTO execution_state
                        (pipeline_run_id, tenant_id, pipeline_id, service_name,
                         current_stage, current_traffic_weight, status, last_updated)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (pipeline_run_id) DO UPDATE SET
                        current_stage = $5,
                        current_traffic_weight = $6,
                        status = $7,
                        last_updated = $8
                    """,
                    state.pipeline_run_id,
                    state.tenant_id,
                    state.pipeline_id,
                    state.service_name,
                    state.current_stage,
                    state.current_traffic_weight,
                    state.status.value if hasattr(state.status, "value") else str(state.status),
                    datetime.fromisoformat(state.last_updated),
                )

    async def get_interrupted_pipelines(self, staleness_seconds: int = 120) -> list[dict]:
        """Cross-tenant by design — see module docstring."""
        async with self._superuser_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pipeline_run_id, tenant_id, pipeline_id, service_name,
                       current_stage, current_traffic_weight, status, last_updated
                FROM execution_state
                WHERE status = 'RUNNING'
                  AND last_updated < NOW() - ($1 || ' seconds')::interval
                """,
                str(staleness_seconds),
            )
            return [dict(r) for r in rows]

    async def get_policy_yaml(self, pipeline_id: str) -> str | None:
        """Cross-tenant read for the same reason as get_interrupted_pipelines: reconciliation runs before any per-request tenant context exists."""
        async with self._superuser_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT policy_yaml FROM pipelines WHERE pipeline_id = $1", pipeline_id)
            return row["policy_yaml"] if row else None

    async def is_first_deployment(self, pipeline_id: str, tenant_id: str) -> bool:
        """
        Real gap found live (2026-09-15): onboarding always assumed a
        baseline version already existed to compare a canary against — a
        project's genuinely first-ever deployment has nothing to compare
        against, so canary_loop's statistical verification is a category
        error for it (matches Argo Rollouts' own documented first-deployment
        behavior: ship straight to 100%, skip analysis, until a real
        previous-known-good version exists to protect). True until
        `mark_first_deployment_completed` is called once for this pipeline.

        Real bug found live (2026-09-15): `set_config(..., true)` is
        transaction-LOCAL — with asyncpg, each bare `execute`/`fetchrow`
        call is its own separate implicit transaction, so the tenant context
        set here was discarded before the SELECT below ever ran, and RLS's
        `::uuid` cast on the resulting empty setting raised
        `invalid input syntax for type uuid: ""` on every single call
        (reproduced directly against a real project, not just in theory).
        Both statements now share one explicit transaction so the setting
        is still live when the SELECT executes.
        """
        async with self._app_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                row = await conn.fetchrow(
                    "SELECT first_deployment_completed_at FROM pipelines WHERE pipeline_id = $1", pipeline_id
                )
            return row is not None and row["first_deployment_completed_at"] is None

    async def mark_first_deployment_completed(self, pipeline_id: str, tenant_id: str) -> None:
        async with self._app_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    "UPDATE pipelines SET first_deployment_completed_at = NOW() "
                    "WHERE pipeline_id = $1 AND first_deployment_completed_at IS NULL",
                    pipeline_id,
                )

    async def record_live_url_verification(self, pipeline_id: str, tenant_id: str, verified: bool) -> None:
        """
        Migration 0014 — real gap this closes: the platform could report a
        project's live_url without ever actually confirming it responds
        (see shared/live_url_check.py's module docstring). Called after
        every real cutover (blue-green, first deployment) with the result
        of a genuine HTTP GET through the real ALB/gateway. Joins through
        `pipelines.pipeline_id` (worker.py always has this, never a raw
        `project_id`) rather than requiring a second id to be threaded
        through every stage-dispatch call site.
        """
        async with self._app_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    "UPDATE projects SET live_url_status = $1, live_url_verified_at = NOW() "
                    "WHERE pipeline_id = $2",
                    "verified" if verified else "failed",
                    pipeline_id,
                )

    async def record_actuation(
        self,
        tenant_id: str,
        pipeline_run_id: str,
        action: str,
        canary_weight: int | None,
        baseline_weight: int | None,
        authorized_by: str,
        hmac_signature: str = "verified",
        verdict: str | None = None,
        confidence: float | None = None,
        policy_rule: str | None = None,
    ) -> None:
        """
        Real gap found live (2026-09-17): worker.py's health-gated blue-green
        path (cutover/rollback/graduate) never wrote a single audit_ledger
        row — the Audit Ledger UI screen correctly showed "0 total actions"
        for a run that had genuinely cut over real production traffic,
        because nothing on this path ever called this. Mirrors
        policy-controller's src/db.py::PolicyControllerDB.record_actuation
        exactly (same table, same columns) — pipeline-worker needs its own
        copy since it has a separate asyncpg pool in a separate Docker build
        context, not because the write itself should differ. `hmac_signature`
        defaults to "verified" (not a real verdict signature) because
        blue-green's whole point is cutting over WITHOUT a statistical
        verdict to sign — matches the same convention FIRST_DEPLOYMENT's own
        (verdict-less) actuation already uses.
        """
        async with self._app_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    """
                    INSERT INTO audit_ledger
                        (tenant_id, pipeline_run_id, action, verdict, confidence,
                         authorized_by, policy_rule, hmac_signature, canary_weight, baseline_weight)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    tenant_id,
                    pipeline_run_id,
                    action,
                    verdict,
                    confidence,
                    authorized_by,
                    policy_rule,
                    hmac_signature,
                    canary_weight,
                    baseline_weight,
                )

    async def record_cost_analysis(
        self,
        tenant_id: str,
        pipeline_run_id: str,
        baseline_cost: float,
        canary_cost: float,
        delta_percent: float,
        rightsizing_rec: dict | None = None,
    ) -> None:
        """
        Real gap found live (2026-09-17): cost_analysis has had 0 rows for
        any blue-green rollout on any project, ever — the only caller of
        the real cost-computation formula was policy-controller's verdict-
        driven controller.py, which blue-green's health-gated path never
        reaches (see shared/cost_formulas.py's module docstring). Mirrors
        policy-controller's src/db.py::PolicyControllerDB.record_cost_analysis
        exactly (same table, same columns) for the same "separate Docker
        build context" reason record_actuation above has its own copy here.
        """
        async with self._app_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    """
                    INSERT INTO cost_analysis
                        (tenant_id, pipeline_run_id, baseline_cost, canary_cost, delta_percent, rightsizing_rec)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    tenant_id,
                    pipeline_run_id,
                    baseline_cost,
                    canary_cost,
                    delta_percent,
                    json.dumps(rightsizing_rec) if rightsizing_rec is not None else None,
                )
