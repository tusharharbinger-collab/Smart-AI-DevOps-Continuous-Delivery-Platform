"""
services/pipeline-worker/tests/test_first_deployment_db_uuid_bug.py

Real bug found live (2026-09-15): `PipelineWorkerDB.is_first_deployment()`
and `mark_first_deployment_completed()` called `set_config('app.active_
tenant_id', $1, true)` and their real query as two SEPARATE bare asyncpg
statements. `set_config(..., true)` ("SET LOCAL" semantics) is
transaction-LOCAL, and each bare `conn.execute`/`conn.fetchrow` call is its
own separate implicit transaction — so the tenant context was discarded
before the real query ran, and RLS's `::uuid` cast on the now-empty setting
raised `invalid input syntax for type uuid: ""` on every single call. This
broke EVERY project's first-ever deployment at the `canary_verify` stage,
right after a real build+test+canary_deploy had already succeeded.

This is a real integration test (needs the actual dev Postgres this repo's
docker-compose brings up) rather than a mock — a fake asyncpg connection
couldn't faithfully reproduce the specific transaction-isolation semantics
that caused the bug, so a mock could pass even with the fix reverted.
No pytest-asyncio dependency (not used elsewhere in this test suite) —
each test drives its own asyncio.run(). Skips cleanly if Postgres isn't
reachable (e.g. running this file outside `docker compose up`).
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

asyncpg = pytest.importorskip("asyncpg")

from src.db import PipelineWorkerDB  # noqa: E402

SUPERUSER_DSN = os.environ.get("POSTGRES_DSN", "postgresql://platform:platform@localhost:5432/platform").replace(
    "postgresql+asyncpg://", "postgresql://"
)
TENANT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


async def _postgres_reachable() -> bool:
    try:
        conn = await asyncpg.connect(SUPERUSER_DSN, timeout=3)
        await conn.close()
        return True
    except Exception:
        return False


async def _make_pipeline_row() -> str:
    pipeline_id = str(uuid.uuid4())
    conn = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await conn.execute(
            "INSERT INTO pipelines (pipeline_id, tenant_id, name, policy_yaml) VALUES ($1, $2, $3, $4)",
            pipeline_id,
            TENANT_ID,
            f"regression-test-{pipeline_id}",
            "apiVersion: delivery.devops.ai/v1alpha1\nkind: Pipeline\n",
        )
    finally:
        await conn.close()
    return pipeline_id


async def _delete_pipeline_row(pipeline_id: str) -> None:
    conn = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await conn.execute("DELETE FROM pipelines WHERE pipeline_id = $1", pipeline_id)
    finally:
        await conn.close()


def _requires_postgres():
    if not asyncio.run(_postgres_reachable()):
        pytest.skip("Postgres not reachable on this host — run via `docker compose up` first.")


def test_is_first_deployment_does_not_raise_invalid_uuid():
    _requires_postgres()

    async def scenario():
        pipeline_id = await _make_pipeline_row()
        db = PipelineWorkerDB()
        await db.connect()
        try:
            return await db.is_first_deployment(pipeline_id, TENANT_ID)
        finally:
            await db.close()
            await _delete_pipeline_row(pipeline_id)

    assert asyncio.run(scenario()) is True


def test_mark_first_deployment_completed_then_is_first_deployment_false():
    _requires_postgres()

    async def scenario():
        pipeline_id = await _make_pipeline_row()
        db = PipelineWorkerDB()
        await db.connect()
        try:
            await db.mark_first_deployment_completed(pipeline_id, TENANT_ID)
            return await db.is_first_deployment(pipeline_id, TENANT_ID)
        finally:
            await db.close()
            await _delete_pipeline_row(pipeline_id)

    assert asyncio.run(scenario()) is False
