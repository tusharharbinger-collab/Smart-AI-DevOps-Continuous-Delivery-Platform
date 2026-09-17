"""
services/pipeline-worker/tests/test_test_stage_non_blocking.py

Real bug found live: a project's test command is user-supplied (or, until
this fix, silently defaulted to a Python-specific "pytest tests/" the
wizard pre-filled regardless of the project's actual language) and runs in
pipeline-worker's OWN container — which doesn't carry every language's
runtime. A wrong/misconfigured command (or one that legitimately finds no
matching tests) used to hard-fail the WHOLE pipeline, even when the
project's own Dockerfile already ran its real tests as a build layer and
that build succeeded. Test commands are now genuinely optional and
non-blocking: a missing one is skipped outright, and a failing one is
recorded but never stops build/deploy.

Same real-infra integration pattern as test_crash_recovery.py (real
Postgres + Redis, self-skips if unreachable) — this exercises
PipelineOrchestrator.start_pipeline directly rather than mocking it,
since the behavior under test is inside that method's own stage loop.
"""
import asyncio
import os
import sys
import uuid

import asyncpg
import pytest
import redis

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import PipelineWorkerDB, SUPERUSER_DSN
from src.pipeline.execution_state import StageStatus
from src.worker import PipelineOrchestrator

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ACME_TENANT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

MANIFEST_TEMPLATE = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: {name}
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: test
      type: test
      config:
{command_line}

  gates:
    blockedDeployWindows: []
    manualApprovalRequired:
      beforeStages: []
      approverRoles: []

  guardrails:
    autoRollbackOnVerdict: ["FAILED"]
    requireMinimumConfidence: 0.80
    minSampleSize: 100
    maxPermittedCostDeltaPercent: 15.0
"""


def _infra_reachable() -> bool:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
    except Exception:
        return False

    async def _check_pg():
        conn = await asyncpg.connect(SUPERUSER_DSN)
        await conn.close()

    try:
        asyncio.run(_check_pg())
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _infra_reachable(), reason="Postgres/Redis not reachable on localhost (docker compose up -d)"
)


def _run_single_test_stage_pipeline(tmp_path, name: str, command_line: str):
    """Writes a minimal one-stage ("test" only) pipeline manifest, registers
    the required pipelines/pipeline_executions rows (same FK setup
    test_crash_recovery.py uses), runs it for real via start_pipeline, and
    returns the final execution_state row plus the in-memory stage result."""
    manifest_path = tmp_path / f"{name}.yaml"
    manifest_path.write_text(
        MANIFEST_TEMPLATE.format(name=name, tenant_id=ACME_TENANT_ID, command_line=command_line),
        encoding="utf-8",
    )
    policy_yaml = manifest_path.read_text(encoding="utf-8")

    pipeline_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    async def _run():
        conn = await asyncpg.connect(SUPERUSER_DSN)
        try:
            await conn.execute(
                "INSERT INTO pipelines (pipeline_id, tenant_id, name, policy_yaml) VALUES ($1, $2, $3, $4)",
                pipeline_id, ACME_TENANT_ID, name, policy_yaml,
            )
            await conn.execute(
                """
                INSERT INTO pipeline_executions
                    (pipeline_run_id, tenant_id, pipeline_id, target_version, status)
                VALUES ($1, $2, $3, $4, 'PENDING')
                """,
                run_id, ACME_TENANT_ID, pipeline_id, "v-test",
            )

            db = PipelineWorkerDB()
            await db.connect()
            redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            redis_client.delete(f"lock:pipeline:{ACME_TENANT_ID}:{name}")

            orchestrator = PipelineOrchestrator(redis_client, db)
            try:
                # Real production code (worker.py's message consumer) always
                # runs start_pipeline via asyncio.to_thread — db.py's
                # run_from_thread relies on THAT thread separation
                # (asyncio.run_coroutine_threadsafe scheduling work back
                # onto the caller's own event-loop thread while this one
                # blocks waiting for the result). Calling start_pipeline
                # directly on the same thread that owns the DB pool's event
                # loop deadlocks that wait until its 10s timeout — confirmed
                # live (db_save_state_failed with an empty error message,
                # concurrent.futures.TimeoutError's own str()) before this
                # fix; the pipeline still completed either way (Postgres
                # persistence is deliberately best-effort — see
                # save_state_sync's own docstring), but the row this test
                # asserts on never got written.
                result = await asyncio.to_thread(
                    orchestrator.start_pipeline,
                    str(manifest_path), pipeline_run_id=run_id, pipeline_id=pipeline_id, tenant_id=ACME_TENANT_ID,
                )
                row = await conn.fetchrow(
                    "SELECT status FROM execution_state WHERE pipeline_run_id = $1", run_id
                )
                return result, row
            finally:
                await db.close()
        finally:
            await conn.execute("DELETE FROM execution_state WHERE pipeline_id = $1", pipeline_id)
            await conn.execute("DELETE FROM pipeline_executions WHERE pipeline_id = $1", pipeline_id)
            await conn.execute("DELETE FROM pipelines WHERE pipeline_id = $1", pipeline_id)
            await conn.close()

    return asyncio.run(_run())


def test_failing_test_command_does_not_fail_the_pipeline(tmp_path):
    result, row = _run_single_test_stage_pipeline(
        tmp_path,
        "test-stage-failure-non-blocking",
        '        command: "python -c \\"import sys; sys.exit(1)\\""',
    )

    assert row is not None
    assert row["status"] == StageStatus.COMPLETED.value, (
        f"a failing test COMMAND must never fail the pipeline — got {row['status']}"
    )
    assert result["results"]["test"]["status"] == "failed_non_blocking"


def test_missing_test_command_is_skipped_not_defaulted_to_pytest(tmp_path):
    result, row = _run_single_test_stage_pipeline(
        tmp_path,
        "test-stage-missing-command-skipped",
        "        {}",  # empty config block — no `command` key at all
    )

    assert row is not None
    assert row["status"] == StageStatus.COMPLETED.value
    assert result["results"]["test"] == {"status": "skipped", "reason": "no test command configured"}
