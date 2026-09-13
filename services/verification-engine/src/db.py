"""
services/verification-engine/src/db.py

verification-engine's first real Postgres connection. Before this, every
verdict this platform ever produced was published to Redis (which is why
the live dashboard has always worked) but NEVER durably written to
`verification_records` — meaning the "Deployment Report" endpoint
(api-gateway's `/api/v1/reports/deployment/{run_id}`) 404'd for every run,
ever, and `GET /api/v1/verification/{run_id}/history` always returned an
empty list. Found live: a user asked why the report showed nothing for a
run that had genuinely, successfully completed.

Uses APP_POSTGRES_DSN (the RLS-enforced `app_user` role — same as every
other service's Postgres access in this platform), with
`app.active_tenant_id` set to the run's own tenant_id for the duration of
the write, matching the per-write RLS discipline already established in
pipeline-worker's and policy-controller's own `db.py` modules.

Deliberately does NOT touch Kubernetes and does not import the
`kubernetes` package — this module only ever talks to Postgres. Adding a
Postgres connection to verification-engine does not weaken invariant #3
(CLAUDE.md) — that invariant is specifically about Kubernetes access, and
nothing here changes that boundary.
"""
import json
import math
import os

import asyncpg
import structlog

logger = structlog.get_logger(__name__)


def _to_asyncpg_dsn(sqlalchemy_style_dsn: str) -> str:
    return sqlalchemy_style_dsn.replace("postgresql+asyncpg://", "postgresql://")


APP_DSN = _to_asyncpg_dsn(
    os.environ.get("APP_POSTGRES_DSN", "postgresql://app_user:app_password@localhost:5432/platform")
)


def _json_safe(obj):
    """
    Same fix as policy-controller's opa_evaluator.py: a verdict's evidence
    can legitimately contain a non-finite float (e.g. Fisher's exact test's
    odds ratio is mathematically infinite whenever a contingency-table cell
    is exactly 0). Postgres's JSONB type rejects the literal `Infinity`/
    `NaN` tokens Python's `json.dumps` would otherwise emit, the same way
    OPA's JSON decoder does — sanitize before it ever reaches a JSON column.
    Duplicated rather than shared: it's a five-line pure function, and the
    two services have no other reason to share a module.
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


class VerificationEngineDB:
    def __init__(self):
        self._pool: asyncpg.Pool | None = None

    async def connect(self):
        self._pool = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=5)
        logger.info("verification_engine_db_connected")

    async def close(self):
        if self._pool:
            await self._pool.close()

    async def record_verdict(self, tenant_id: str, verdict, hmac_signature: str) -> None:
        """
        `verdict` is an `ImmutableVerdict` (verdict.py). `rca_summary` is
        left NULL here — it's generated asynchronously, after the fact, by
        policy-controller once it has finished acting on the verdict (RCA
        generation must never delay or block the actual safety decision;
        see explainability-service/src/report_generator.py's docstring).

        Requires a `pipeline_executions` row for this run to already exist
        (a real FK) — true for every run that went through the normal
        trigger flow, but NOT true for a bare `/verify` call made directly
        (e.g. an adversarial test hitting this endpoint without ever going
        through api-gateway's trigger_run). That case is caught and logged,
        never allowed to fail the actual verification response.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.active_tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    """
                    INSERT INTO verification_records
                        (verdict_id, tenant_id, pipeline_run_id, status, composite_score,
                         confidence, evidence, tier1_breaches, hmac_signature)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                    """,
                    verdict.verdict_id,
                    tenant_id,
                    verdict.pipeline_run_id,
                    verdict.status,
                    verdict.composite_score,
                    verdict.confidence,
                    json.dumps(_json_safe(verdict.evidence)),
                    list(verdict.tier1_breaches),
                    hmac_signature,
                )
