"""
services/policy-controller/src/rollout_scheduler.py

Real gap found live: a HEALTHY verdict always promoted the canary straight
to a hardcoded 25% traffic weight (`verdict.get("recommended_weight", 25)`
in controller.py — nothing ever set `recommended_weight`), and the
pipeline was marked COMPLETED after that single verification cycle no
matter how many steps a pipeline's `canary_loop.steps` (10% -> 25% -> 50%
-> 100%, each with its own real minSampleSize/minDuration) actually
declared. There was no automated ramp at all — just one fixed-size jump.

This module owns the per-run rollout state that makes a real ramp
possible: `rollout_state:{run_id}` in Redis, written once by pipeline-
worker at pipeline start (`worker.py::_register_actuation_target`) with
the parsed step schedule, and updated here by controller.py after every
successful promotion. Advancing to the next step means waiting out THAT
step's own real minDuration (never a flat guess) and then asking
pipeline-worker's `/pipelines/{run_id}/reverify` to produce the next
verdict — never touching Kubernetes here, since actuation stays
policy-controller's `handle_incoming_verdict` path exclusively.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger(__name__)

PIPELINE_WORKER_URL = os.environ.get("PIPELINE_WORKER_URL", "http://pipeline-worker:8001")
API_GATEWAY_URL = os.environ.get("API_GATEWAY_URL", "http://api-gateway:8000")

_DURATION_RE = re.compile(r"^(\d+)(s|m)$")


def parse_duration_seconds(duration_str: str) -> int:
    """Small local copy of pipeline-worker's schemas.py::parse_duration_seconds —
    separate services, separate dependency trees, no shared import path."""
    match = _DURATION_RE.match((duration_str or "0s").strip())
    if not match:
        return 0
    value, unit = match.groups()
    seconds = int(value)
    return seconds * 60 if unit == "m" else seconds


async def get_rollout_state(redis_client, run_id: str) -> dict | None:
    if redis_client is None:
        return None
    raw = await redis_client.get(f"rollout_state:{run_id}")
    return json.loads(raw) if raw else None


async def save_rollout_state(redis_client, run_id: str, state: dict) -> None:
    if redis_client is None:
        return
    await redis_client.set(f"rollout_state:{run_id}", json.dumps(state), ex=86400)


def step_elapsed_seconds(state: dict) -> float:
    started_at = state.get("step_started_at")
    if not started_at:
        return 0.0
    started = datetime.fromisoformat(started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


async def advance_to_next_step(
    redis_client,
    run_id: str,
    tenant_id: str | None,
    state: dict,
    next_index: int,
    trace_id: str | None = None,
) -> None:
    """
    Persists the new step index/start-time, then schedules the next
    verification cycle to fire once that step's own real minDuration has
    elapsed — fully autonomous, but never faster than the policy's own
    evidence-sufficiency requirement (no static/guessed dwell time).
    """
    next_step = state["steps"][next_index]
    state["current_step_index"] = next_index
    state["step_started_at"] = datetime.now(timezone.utc).isoformat()
    state["status"] = "RUNNING"
    await save_rollout_state(redis_client, run_id, state)

    delay_seconds = max(0, next_step.get("minDurationSeconds", 0))
    logger.info(
        "rollout_step_advance_scheduled",
        pipeline_run_id=run_id,
        next_step_index=next_index,
        target_weight=next_step.get("trafficWeight"),
        delay_seconds=delay_seconds,
    )
    asyncio.create_task(
        _fire_reverify_after_delay(run_id, tenant_id, state["verification_config"], delay_seconds, trace_id)
    )


async def _fire_reverify_after_delay(
    run_id: str, tenant_id: str | None, verification_config: dict, delay_seconds: float, trace_id: str | None
) -> None:
    """
    Fire-and-forget background task (never blocks verdict handling for the
    CURRENT step). A failure here just means this run's ramp stalls at its
    current, already-verified-safe weight — logged loudly rather than
    silently losing the next step forever.
    """
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/pipelines/{run_id}/reverify",
                json={
                    "tenant_id": tenant_id,
                    "verification_config": verification_config,
                    "elapsed_seconds": delay_seconds,
                    "trace_id": trace_id,
                },
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error("rollout_step_reverify_failed", pipeline_run_id=run_id, error=str(e))


async def graduate(run_id: str, tenant_id: str | None, new_version: str | None) -> None:
    """
    Real gap found live: nothing anywhere updated a project's baseline
    version once its canary reached 100% — the NEXT release would still be
    compared against the ORIGINAL baseline forever, defeating the point of
    a continuous-delivery loop. Tells api-gateway (the sole owner of the
    `projects` table) that this run's canary is now the production version.
    Best-effort: a failure here doesn't undo the traffic promotion that
    already happened, it just means the next rollout's baseline bookkeeping
    is stale and worth checking manually.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_GATEWAY_URL}/api/v1/projects/internal/pipeline-runs/{run_id}/graduate",
                json={"tenant_id": tenant_id, "new_version": new_version},
            )
            resp.raise_for_status()
        logger.info("canary_graduated_to_baseline", pipeline_run_id=run_id, new_version=new_version)
    except Exception as e:
        logger.error("canary_graduation_failed", pipeline_run_id=run_id, error=str(e))
