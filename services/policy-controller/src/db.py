"""
services/policy-controller/src/db.py

policy-controller's first real Postgres connection. Before this,
`audit_writer.record_actuation()` had a Postgres write path
(`if db_session: ...`) that no caller ever actually supplied a session
for — `update_traffic_weights`/`emergency_rollback` call `record_actuation`
with no `db_session` argument at all, so every real actuation this
platform has ever performed only ever logged to stdout. The `audit_ledger`
table (and everything reading it — the Audit Ledger UI screen, the SOC 2
CSV export) has been empty since day one. Found live: a user noticed the
Audit Ledger tab was blank for a run that had genuinely completed.

Uses APP_POSTGRES_DSN (the RLS-enforced `app_user` role, same as
api-gateway and pipeline-worker) — never the superuser DSN — with
`app.active_tenant_id` set to the specific run's own tenant_id for the
duration of each write, matching the per-request RLS discipline
api-gateway's middleware uses (see that module's docstring) and the same
per-write pattern pipeline-worker's `db.py` already established for
`execution_state`.
"""
import os

import asyncpg
import structlog

logger = structlog.get_logger(__name__)


def _to_asyncpg_dsn(sqlalchemy_style_dsn: str) -> str:
    return sqlalchemy_style_dsn.replace("postgresql+asyncpg://", "postgresql://")


APP_DSN = _to_asyncpg_dsn(
    os.environ.get("APP_POSTGRES_DSN", "postgresql://app_user:app_password@localhost:5432/platform")
)


class PolicyControllerDB:
    def __init__(self):
        self._pool: asyncpg.Pool | None = None

    async def connect(self):
        self._pool = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=5)
        logger.info("policy_controller_db_connected")

    async def close(self):
        if self._pool:
            await self._pool.close()

    async def record_actuation(
        self,
        tenant_id: str,
        pipeline_run_id: str,
        action: str,
        canary_weight: int | None,
        baseline_weight: int | None,
        authorized_by: str,
        verdict: str | None,
        confidence: float | None,
        policy_rule: str | None,
        hmac_signature: str,
    ) -> None:
        async with self._pool.acquire() as conn:
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

    async def update_rca_summary(self, tenant_id: str, verdict_id: str, rca_summary: str) -> None:
        """
        Fills in the RCA explanation generated asynchronously, after the
        actual actuation decision — see rca_trigger.py's module docstring
        for why this happens as a fire-and-forget follow-up rather than
        inline during verdict processing. RLS still requires
        `app.active_tenant_id` set for this UPDATE to see/affect the row at
        all (verification_records has the same tenant-scoped RLS policy as
        everything else) even though `verdict_id` alone is already globally
        unique as the primary key.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    "UPDATE verification_records SET rca_summary = $1 WHERE verdict_id = $2",
                    rca_summary,
                    verdict_id,
                )
