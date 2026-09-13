"""
services/pipeline-worker/src/tasks/rollout_task.py

Orchestrates progressive canary steps (10% -> 25% -> 50% -> 100%).
Spec §4.1 & §5.2.
"""
import time
import structlog
from src.pipeline.execution_state import PipelineExecutionState, StageStatus, ExecutionStateStore

logger = structlog.get_logger(__name__)


def run_rollout_task(
    pipeline_run_id: str,
    steps: list[dict],
    current_step_idx: int = 0,
    state_store: ExecutionStateStore | None = None,
) -> dict:
    """
    Executes or advances traffic progression steps according to the declarative policy schedule.
    """
    if current_step_idx >= len(steps):
        logger.info("all_steps_completed", pipeline_run_id=pipeline_run_id)
        return {"status": "COMPLETED", "weight": 100}

    step = steps[current_step_idx]
    target_weight = step["trafficWeight"]
    min_sample_size = step.get("minSampleSize", 100)
    requires_approval = step.get("requiresManualApproval", False)

    logger.info(
        "advancing_rollout_step",
        pipeline_run_id=pipeline_run_id,
        step_index=current_step_idx,
        traffic_weight=target_weight,
        requires_approval=requires_approval,
    )

    if requires_approval:
        return {
            "status": "AWAITING_APPROVAL",
            "step_index": current_step_idx,
            "target_weight": target_weight,
        }

    return {
        "status": "RUNNING",
        "step_index": current_step_idx,
        "traffic_weight": target_weight,
        "min_sample_size": min_sample_size,
    }
