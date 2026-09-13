"""
services/pipeline-worker/tests/test_crash_recovery.py

Phase 5 (§05-reliability-scale.md, deliverable 5.2): the actual claim
reconciler.py's docstring makes — "resumes a pipeline left RUNNING by a
crashed worker, from its last recorded stage" — verified for real against
real Redis + real Postgres, not just by reading the code. Requires the
docker-compose stack running (`docker compose up -d`); skips itself
(not a failure) if Postgres/Redis aren't reachable on localhost.

Scenario: a 2-stage pipeline ("test" -> "progressive_verify") is simulated
as having crashed WHILE the second stage was in progress — the first stage
already completed, but the worker died before finishing (or acking) the
second. A fresh PipelineOrchestrator + reconciler is exercised exactly as
main.py's periodic reconcile loop would, and this test asserts:
  1. the interrupted run is found and picked up at all
  2. "test" (the already-completed stage) is NOT re-executed — the
     resumed run only ever had the second stage passed to `start_pipeline`
  3. the pipeline reaches COMPLETED — a real resumed rollout, not a stub

A single plain (non-async) test function driving one `asyncio.run()` —
pipeline-worker's test suite has no pytest-asyncio dependency, and adding
one for a single test isn't worth it.

Run it from INSIDE the pipeline-worker container, not the host: this dev
machine has a native Windows Postgres service already bound to port 5432,
which wins the race for `localhost:5432` ahead of Docker Desktop's forwarded
port — host-side tools silently talk to the wrong Postgres server entirely
(confirmed via `tasklist`; unrelated to any code in this repo). Redis has no
such conflict. Inside the container, `POSTGRES_DSN`/`REDIS_URL` already
point at the right place via the compose network:
    docker compose exec pipeline-worker pip install pytest
    docker compose exec pipeline-worker python -m pytest tests/test_crash_recovery.py -v
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import redis

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import PipelineWorkerDB, SUPERUSER_DSN
from src.pipeline.execution_state import ExecutionStateStore, StageStatus
from src.pipeline.reconciler import reconcile_interrupted_pipelines
from src.worker import PipelineOrchestrator

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ACME_TENANT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

TEST_MANIFEST_YAML = """\
apiVersion: delivery.devops.ai/v1alpha1
kind: Pipeline
metadata:
  name: crash-recovery-test
  tenantId: "{tenant_id}"
  namespace: production

spec:
  stages:
    - name: test
      type: test
      config:
        command: "python -c \\"pass\\""

    - name: progressive_verify
      type: canary_loop
      dependsOn: [test]
      config:
        gatewayRef: local-edge-gateway
        service: crash-recovery-test
        routeName: payment-service-route
        canaryDeployment: payment-service-canary
        steps:
          - trafficWeight: 10
            minDuration: 0s
            minSampleSize: 0

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

  verificationConfig:
    minEvaluationWindowSeconds: 1
    metrics:
      - name: crash_recovery_error_rate
        category: error_rate
        tier: critical
        alpha: 0.01
        p0: 0.005
        p1: 0.020
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


def test_resumes_from_the_stage_it_crashed_on_not_from_scratch(tmp_path):
    manifest_path = tmp_path / "crash-recovery-test.yaml"
    manifest_path.write_text(TEST_MANIFEST_YAML.format(tenant_id=ACME_TENANT_ID), encoding="utf-8")
    policy_yaml = manifest_path.read_text(encoding="utf-8")

    pipeline_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    async def _run():
        conn = await asyncpg.connect(SUPERUSER_DSN)
        try:
            await conn.execute(
                "INSERT INTO pipelines (pipeline_id, tenant_id, name, policy_yaml) VALUES ($1, $2, $3, $4)",
                pipeline_id, ACME_TENANT_ID, f"crash-recovery-test-{pipeline_id[:8]}", policy_yaml,
            )
            # execution_state.pipeline_run_id has a FK to pipeline_executions
            # — mirrors what api-gateway's real trigger_run() inserts before
            # ever enqueuing the run.
            await conn.execute(
                """
                INSERT INTO pipeline_executions
                    (pipeline_run_id, tenant_id, pipeline_id, target_version, status)
                VALUES ($1, $2, $3, $4, 'RUNNING')
                """,
                run_id, ACME_TENANT_ID, pipeline_id, "v-test",
            )

            # Simulate a worker having died partway through
            # "progressive_verify" — "test" already completed (that's why
            # current_stage is the SECOND stage, not the first), and the
            # row is stale enough to count as interrupted.
            stale_timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)
            await conn.execute(
                """
                INSERT INTO execution_state
                    (pipeline_run_id, tenant_id, pipeline_id, service_name,
                     current_stage, current_traffic_weight, status, last_updated)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                run_id, ACME_TENANT_ID, pipeline_id, "crash-recovery-test",
                "progressive_verify", 0, "RUNNING", stale_timestamp,
            )

            db = PipelineWorkerDB()
            await db.connect()
            redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            # A prior run's tenant/service lock (real TTL up to 1h) must not
            # leak into this test and make reconciliation spuriously blocked.
            redis_client.delete(f"lock:pipeline:{ACME_TENANT_ID}:crash-recovery-test")

            orchestrator = PipelineOrchestrator(redis_client, db)
            state_store = ExecutionStateStore(redis_client, db)

            try:
                resumed_count = await reconcile_interrupted_pipelines(state_store, orchestrator)
                assert resumed_count >= 1, "expected the interrupted pipeline to be found and resumed"

                row = await conn.fetchrow(
                    "SELECT status, current_stage FROM execution_state WHERE pipeline_run_id = $1", run_id
                )
                assert row is not None
                assert row["status"] == StageStatus.COMPLETED.value, (
                    f"expected the resumed pipeline to reach COMPLETED, got {row['status']}"
                )
                # If "test" (a subprocess call) had been re-executed,
                # current_stage would have passed through it — instead it
                # should only ever have been set to "progressive_verify"
                # (resumed directly there) and stay there as the final stage.
                assert row["current_stage"] == "progressive_verify"
            finally:
                await db.close()
        finally:
            await conn.execute("DELETE FROM execution_state WHERE pipeline_id = $1", pipeline_id)
            await conn.execute("DELETE FROM pipeline_executions WHERE pipeline_id = $1", pipeline_id)
            await conn.execute("DELETE FROM pipelines WHERE pipeline_id = $1", pipeline_id)
            await conn.close()

    asyncio.run(_run())
