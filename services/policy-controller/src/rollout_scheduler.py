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
verdict.

`graduate()` is the one function here that DOES touch Kubernetes (via
actuation_executor.graduate_canary — still the only module permitted to
mutate cluster state) — see its own docstring for why a database label
alone wasn't enough.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone

import httpx
import structlog

from src.actuation_executor import graduate_canary
from src.aws_actuation_executor import graduate_canary_ecs

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
    target: dict | None = None,
) -> None:
    """
    Persists the new step index/start-time, then schedules the next
    verification cycle to fire once that step's own real minDuration has
    elapsed — fully autonomous, but never faster than the policy's own
    evidence-sufficiency requirement (no static/guessed dwell time).

    `target` (P1, 2026-09-16 — CloudWatch telemetry) carries this run's
    real deployment_target/aws_region/canary_deployment_name, already
    resolved once by controller.py's own `_get_actuation_target` — threaded
    through to the reverify call so an AWS ECS project's SECOND-and-later
    verdicts use real CloudWatch telemetry too, not just its first.
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
        _fire_reverify_after_delay(run_id, tenant_id, state["verification_config"], delay_seconds, trace_id, target)
    )


async def schedule_retry_of_current_step(
    run_id: str,
    tenant_id: str | None,
    state: dict,
    remaining_seconds: float,
    trace_id: str | None = None,
    target: dict | None = None,
) -> None:
    """
    Real gap found live: a pipeline with no separate deploy/wait stage
    (canary_verify runs immediately after registration — true of every
    project this session's onboarding wizard generates, and of the
    original payments-pipeline demo) produces its FIRST verdict with
    essentially zero real elapsed time. OPA's own minDuration/minSampleSize
    gate (correctly, now that it sees REAL evidence instead of the old
    hardcoded stand-ins — see _build_promotion_opa_input) rejects that
    first attempt every time. Before this, that rejection just fell into
    the generic BLOCKED alert and the run ended there — the ramp could
    never even complete its FIRST step for any pipeline whose evidence
    literally cannot exist yet, a silent dead end. This does what a human
    operator obviously would: wait out however much of THIS step's own gate
    is still unmet, then ask for the verdict again — the SAME step, so
    (unlike advance_to_next_step) `step_started_at` is deliberately left
    untouched, letting real elapsed time keep accumulating from when this
    step genuinely began rather than restarting the clock.
    """
    logger.info(
        "rollout_step_retry_scheduled",
        pipeline_run_id=run_id,
        step_index=state["current_step_index"],
        remaining_seconds=remaining_seconds,
    )
    asyncio.create_task(
        _fire_reverify_after_delay(run_id, tenant_id, state["verification_config"], remaining_seconds, trace_id, target)
    )


async def _fire_reverify_after_delay(
    run_id: str,
    tenant_id: str | None,
    verification_config: dict,
    delay_seconds: float,
    trace_id: str | None,
    target: dict | None = None,
) -> None:
    """
    Fire-and-forget background task (never blocks verdict handling for the
    CURRENT step). A failure here just means this run's ramp stalls at its
    current, already-verified-safe weight — logged loudly rather than
    silently losing the next step forever.
    """
    target = target or {}
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
                    # CloudWatch telemetry (P1, 2026-09-16) — an AWS ECS
                    # project's SECOND-and-later verdicts need the same real
                    # deployment_target/aws_region/service_name its FIRST
                    # one already gets via worker.py's own canary_loop
                    # config; this is the one place that context has to
                    # cross the policy-controller -> pipeline-worker
                    # boundary for a reverify. `target` has no plain
                    # service_name field (see _register_actuation_target) —
                    # canary_deployment_name doubles as the real ECS service
                    # name for an AWS project too (aws_ecs_actuation.py's
                    # own f"{service_name}-canary" convention), so stripping
                    # its "-canary" suffix recovers the base name reliably
                    # for exactly the deployment_target this field is ever
                    # read for.
                    "deployment_target": target.get("deployment_target", "kubernetes"),
                    "aws_region": target.get("aws_region"),
                    "ecs_service_name": (target.get("canary_deployment_name") or "").removesuffix("-canary") or None,
                },
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error("rollout_step_reverify_failed", pipeline_run_id=run_id, error=str(e))


async def graduate(run_id: str, target: dict, new_version: str | None, db=None) -> str:
    """
    Real gap found live: reaching 100% never made that durable on the
    cluster — the `canary` Deployment kept serving all traffic while
    `baseline` sat idle running the ORIGINAL image forever, and a database
    label alone (`projects.active_production_tag`) could claim a version
    was "live" while the actual baseline Deployment never ran it. This
    does the REAL work first: actuation_executor.graduate_canary() reads
    the canary's live image and copies it onto baseline (the only
    Kubernetes-mutating step here — actuation stays exclusively there),
    resets traffic to 100% baseline, and idles the canary down. Only if
    THAT succeeds does this tell api-gateway (the sole owner of the
    `projects` table) to record the new production version — so the
    database never claims a version is live when it isn't actually
    running anywhere. Returns "GRADUATED" or "GRADUATION_FAILED" so the
    caller can persist which one actually happened instead of assuming.
    """
    try:
        if target.get("deployment_target", "kubernetes") == "aws_ecs":
            result = await graduate_canary_ecs(
                run_id,
                authorized_by="SYSTEM:auto_graduate",
                service_name=target["canary_deployment_name"].removesuffix("-canary"),
                path_prefix=target["path_prefix"],
                region=target.get("aws_region", "us-east-1"),
                tenant_id=target.get("tenant_id"),
                db=db,
            )
        else:
            result = await graduate_canary(
                run_id,
                authorized_by="SYSTEM:auto_graduate",
                route_name=target["route_name"],
                namespace=target["namespace"],
                canary_deployment_name=target["canary_deployment_name"],
                baseline_deployment_name=target["baseline_deployment_name"],
                tenant_id=target.get("tenant_id"),
                db=db,
            )
    except Exception as e:
        logger.error("canary_graduation_kubernetes_step_failed", pipeline_run_id=run_id, error=str(e))
        return "GRADUATION_FAILED"

    # The image actually running (from the live cluster read inside
    # graduate_canary) is the source of truth for what got recorded as the
    # new baseline — not the requested target_version, which could have
    # drifted from what genuinely built and deployed.
    recorded_version = result.get("new_baseline_image") or new_version

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_GATEWAY_URL}/api/v1/projects/internal/pipeline-runs/{run_id}/graduate",
                json={"tenant_id": target.get("tenant_id"), "new_version": recorded_version},
            )
            resp.raise_for_status()
        logger.info("canary_graduated_to_baseline", pipeline_run_id=run_id, new_version=recorded_version)
        return "GRADUATED"
    except Exception as e:
        # The cluster is already correct at this point — only the
        # project's tracked version bookkeeping is stale, worth checking
        # manually but not worth treating as a failed graduation.
        logger.error("canary_graduation_db_record_failed", pipeline_run_id=run_id, error=str(e))
        return "GRADUATED"
