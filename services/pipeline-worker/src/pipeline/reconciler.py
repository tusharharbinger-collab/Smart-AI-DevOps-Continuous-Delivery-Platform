"""
services/pipeline-worker/src/pipeline/reconciler.py

Runs on pipeline-worker container startup (and periodically thereafter).
Resumes any pipeline left in RUNNING state by a crashed worker, picking up
from the exact stage it was on rather than restarting from stage 0.
Spec §4.3; Phase 5 (§05-reliability-scale.md, deliverable 5.2).

Before this phase, this module existed but was structurally incapable of
working: `get_interrupted_pipelines()` always returned `[]` (no caller ever
wired a real `db_session` into `PipelineOrchestrator`), and even if it had
found something, `resume_fn(pipeline.pipeline_run_id)` called
`run_verification_task`/`run_rollout_task` with only one argument each —
both require a `verificationConfig`/`steps` argument that was never
supplied, so this would have raised a `TypeError` on the very first call.
Both gaps are fixed here: pipeline-worker now has a real Postgres
connection (`src/db.py`), and this module re-fetches the interrupted run's
own registered manifest to resume it with a full `PipelineOrchestrator.
start_pipeline` call — the exact same code path a fresh trigger uses,
just re-entered partway through instead of from stage 0.
"""
import asyncio
import tempfile

import structlog

from src.pipeline.execution_state import ExecutionStateStore

logger = structlog.get_logger(__name__)


async def reconcile_interrupted_pipelines(state_store: ExecutionStateStore, orchestrator) -> int:
    """
    Finds every pipeline left RUNNING by a now-dead worker and resumes each
    from its last recorded stage. Returns how many were resumed (0 is the
    common case — nothing to do — and is not an error).
    """
    interrupted = await state_store.get_interrupted_pipelines()
    resumed_count = 0

    for pipeline in interrupted:
        logger.warning(
            "resuming_interrupted_pipeline",
            pipeline_run_id=pipeline.pipeline_run_id,
            stage=pipeline.current_stage,
            pipeline_id=pipeline.pipeline_id,
        )

        if not pipeline.pipeline_id:
            # Pre-Phase-5 rows (or a run triggered before pipeline_id was
            # threaded through) have no way to look up what to resume as —
            # log and skip rather than guess.
            logger.error(
                "cannot_resume_pipeline_missing_pipeline_id",
                pipeline_run_id=pipeline.pipeline_run_id,
            )
            continue

        policy_yaml = await orchestrator.db.get_policy_yaml(pipeline.pipeline_id)
        if not policy_yaml:
            logger.error(
                "cannot_resume_pipeline_manifest_not_found",
                pipeline_run_id=pipeline.pipeline_run_id,
                pipeline_id=pipeline.pipeline_id,
            )
            continue

        # A crashed worker's own tenant/service concurrency lock (up to a
        # 1h TTL) would otherwise block this same pipeline from resuming —
        # it's provably stale (the run it was guarding is the one we're
        # about to resume), so clear it before re-entering start_pipeline.
        state_store.release_tenant_lock(pipeline.tenant_id, pipeline.service_name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
            tmp.write(policy_yaml)
            manifest_path = tmp.name

        try:
            # off the event loop — start_pipeline blocks for the whole
            # resumed run, same as a fresh trigger (main.py's consumer
            # already does this for that reason).
            await asyncio.to_thread(
                orchestrator.start_pipeline,
                manifest_path,
                pipeline_run_id=pipeline.pipeline_run_id,
                pipeline_id=pipeline.pipeline_id,
                tenant_id=pipeline.tenant_id,
                resume_from_stage=pipeline.current_stage,
            )
            resumed_count += 1
        except Exception as e:
            logger.error(
                "pipeline_resume_failed",
                pipeline_run_id=pipeline.pipeline_run_id,
                stage=pipeline.current_stage,
                error=str(e),
            )

    if resumed_count:
        logger.info("reconciliation_complete", resumed_count=resumed_count)
    return resumed_count
