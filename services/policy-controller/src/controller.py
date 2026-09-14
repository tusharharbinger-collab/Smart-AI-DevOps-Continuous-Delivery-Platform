"""
services/policy-controller/src/controller.py

Policy Controller main loop per spec §8.3:
Reads verdicts from a Redis Stream consumer group (Phase 5 — see
run_policy_controller_loop's docstring), verifies each verdict's HMAC,
evaluates OPA guardrails, and executes traffic actuation via
actuation_executor.
"""
import os
import json
import asyncio
import socket
import redis.asyncio as aioredis
import structlog

from shared import redis_streams as streams
from shared.logging_config import bind_request_context, clear_request_context
from src.verdict_verifier import verify_and_parse, VerdictIntegrityError
from src.opa_evaluator import evaluate_policy_async
from src.actuation_executor import (
    update_traffic_weights,
    emergency_rollback,
    DEFAULT_ROUTE_NAME,
    DEFAULT_NAMESPACE,
    DEFAULT_CANARY_DEPLOYMENT,
    DEFAULT_BASELINE_DEPLOYMENT,
)
from src.alert_dispatcher import send_alert, alert_rollback, alert_verification_timeout, alert_approval_required
from src.rca_trigger import trigger_rca_async
from src.cost_tracker import compute_and_record_cost
from src import rollout_scheduler

logger = structlog.get_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _extract_sample_counts(verdict: dict, policy: dict) -> tuple[int, int]:
    """
    Best-effort (samples_collected, samples_required) for the verification-
    timeout alert. Evidence is nested per-metric (`{"metric_name": {...}}`),
    not a flat dict, so this sums whatever request-count-shaped field each
    metric actually carries (`total_requests` for error_rate/Wald-SPRT
    metrics, `n_canary` for latency's Mann-Whitney/KS metrics) rather than
    assuming one fixed key across every metric category.
    """
    evidence = verdict.get("evidence") or {}
    collected = 0
    for metric_result in evidence.values():
        if not isinstance(metric_result, dict):
            continue
        collected += metric_result.get("total_requests", 0) or 0
        collected += (metric_result.get("mann_whitney") or {}).get("n_canary", 0) or 0
    required = policy.get("guardrails", {}).get("minSampleSize", 100)
    return collected, required


def _build_promotion_opa_input(
    verdict: dict, policy: dict, rollout_state: dict | None, cost_delta_percent: float, approved_signatures: list
) -> tuple[dict, dict | None]:
    """
    Real gap found live: this used to hand OPA a completely made-up picture
    of the active step — `active_step_sample_count` defaulted to a flat
    150, `active_step_duration_seconds` was a hardcoded 300, `current_step`
    was always `{"minSampleSize": 100, "minDuration": "120s"}`, and
    `target_stage` was the same fixed string `"step_promote"` for every
    promotion regardless of which real step (10%? 100%?) was actually being
    evaluated. That made the sample-size/duration/manual-approval gates in
    delivery_guardrails.rego structurally unable to ever see real evidence.

    When `rollout_state` exists (see worker.py's `_register_actuation_target`
    and rollout_scheduler.py), this builds the real picture instead: the
    actual step being promoted to, real elapsed time at that step, and a
    `target_stage` derived from its real traffic weight so the manual-
    approval gate (`beforeStages: [step_100_promotion]`) can genuinely
    match. Falls back to the old flat defaults when no rollout_state is
    registered (a pre-Phase-9 or manually-invoked run), so nothing that
    worked before this regresses.
    """
    collected, _ = _extract_sample_counts(verdict, policy)

    if rollout_state is None:
        return (
            {
                "active_step_sample_count": verdict.get("evidence", {}).get("total_requests", 150),
                "active_step_duration_seconds": 300,
                "current_step": {"minSampleSize": 100, "minDuration": "120s"},
                "target_stage": "step_promote",
                "approved_signatures": approved_signatures,
                "cost_analysis": {"delta_percent": cost_delta_percent},
            },
            None,
        )

    steps = rollout_state["steps"]
    idx = rollout_state["current_step_index"]
    active_step = steps[idx]
    elapsed = rollout_scheduler.step_elapsed_seconds(rollout_state)
    return (
        {
            "active_step_sample_count": collected or verdict.get("evidence", {}).get("total_requests", 0),
            "active_step_duration_seconds": elapsed,
            "current_step": {
                "minSampleSize": active_step["minSampleSize"],
                "minDuration": f"{active_step['minDurationSeconds']}s",
            },
            "target_stage": f"step_{active_step['trafficWeight']}_promotion",
            "approved_signatures": approved_signatures,
            "cost_analysis": {"delta_percent": cost_delta_percent},
        },
        active_step,
    )


async def _get_actuation_target(redis_client, pipeline_run_id: str) -> dict:
    """
    Looks up WHICH service's route/namespace/deployment this run's verdict
    should actuate against. `pipeline-worker` writes this at pipeline start
    (see `worker.py::PipelineOrchestrator.start_pipeline`) since it's the one
    place that already parsed the pipeline's manifest — the policy controller
    never parses pipeline YAML itself, it just looks up the mapping by run id.
    Falls back to the original single-service defaults if a run predates this
    (or the key already expired), so the original demo pipeline keeps working.
    """
    raw = await redis_client.get(f"actuation_target:{pipeline_run_id}") if redis_client else None
    if not raw:
        return {
            "route_name": DEFAULT_ROUTE_NAME,
            "namespace": DEFAULT_NAMESPACE,
            "canary_deployment_name": DEFAULT_CANARY_DEPLOYMENT,
            "baseline_deployment_name": DEFAULT_BASELINE_DEPLOYMENT,
            "tenant_id": None,
        }
    target = json.loads(raw)
    target.setdefault("tenant_id", None)
    # A run registered before baseline_deployment_name existed (see
    # worker.py::_register_actuation_target) — re-derive it from the
    # canary name using manifest_generator.py's own naming convention
    # (f"{service_name}-canary" / f"{service_name}-baseline") rather than
    # falling back to the single-service DEFAULT_*, which would silently
    # point cost_tracker.py at the wrong service's Deployment.
    target.setdefault(
        "baseline_deployment_name",
        target["canary_deployment_name"].replace("-canary", "-baseline"),
    )
    return target


async def handle_incoming_verdict(
    raw_message: str,
    pipeline_run_id: str,
    pipeline_policy: dict | None = None,
    redis_client=None,
    db=None,
):
    """
    Process an incoming signed verdict from the verification engine:
    1. Cryptographic gate: HMAC signature & freshness check
    2. Structural gate: OPA policy evaluation
    3. Actuation: Emergency rollback or traffic progression

    Phase 5 (§05-reliability-scale.md, deliverable 5.4) — "OPA unreachable ->
    fail closed" is NOT a uniformly safe default. Blocking an autonomous
    PROMOTE_STEP when OPA can't be reached is safe (worst case: a healthy
    canary stays at its current, already-verified-safe weight one cycle
    longer). Blocking an autonomous ROLLBACK when OPA can't be reached is
    NOT safe — verification-engine already determined this canary FAILED;
    refusing to roll it back because the guardrail check itself is down
    means a known-bad canary keeps serving real traffic, the opposite of
    what "fail safe" should mean here. So: PROMOTE_STEP stays fail-closed
    (denied) when OPA is unreachable; ROLLBACK fails OPEN (the rollback
    proceeds anyway) — but only when OPA is genuinely unreachable, never
    when OPA was reached and answered "no" for a real policy reason (e.g. a
    freeze window) — `opa_unreachable` (opa_evaluator.py) is what
    distinguishes those two cases.
    """
    try:
        payload = json.loads(raw_message)
    except Exception as e:
        logger.error("invalid_verdict_json", error=str(e), pipeline_run_id=pipeline_run_id)
        return

    try:
        verdict = verify_and_parse(payload)  # Cryptographic gate #1
    except VerdictIntegrityError as e:
        await send_alert("SECURITY", f"Rejected unverified verdict for {pipeline_run_id}: {e}")
        return  # STOP — never reaches OPA, never reaches Kubernetes

    # Real gap found live: this parameter was NEVER supplied by
    # _handle_stream_payload (the only real caller in the actual verdict-
    # processing loop) — every verdict, for every pipeline, was evaluated
    # against this generic hardcoded default regardless of what the
    # pipeline's own YAML actually declared. Most damagingly,
    # `manualApprovalRequired.beforeStages` defaulting to `[]` meant a step
    # flagged `requiresManualApproval` could never actually be gated (OPA's
    # Rule 4 only fires when target_stage appears in that list) — the
    # approval-pause this module implements would have silently never
    # triggered for a single real pipeline. rollout_state (registered by
    # worker.py at pipeline start) now carries the pipeline's REAL
    # gates/guardrails, so that's checked first; the hardcoded default
    # remains only for a legacy run with no rollout_state registered.
    rollout_state = await rollout_scheduler.get_rollout_state(redis_client, pipeline_run_id)
    policy = (
        pipeline_policy
        or (rollout_state.get("pipeline_policy") if rollout_state else None)
        or {
            "gates": {"blockedDeployWindows": [], "manualApprovalRequired": {"beforeStages": [], "approverRoles": []}},
            "guardrails": {
                "autoRollbackOnVerdict": ["FAILED"],
                "requireMinimumConfidence": 0.80,
                "minSampleSize": 100,
                "maxPermittedCostDeltaPercent": 15.0,
            },
        }
    )

    if verdict.get("status") == "UNVERIFIABLE":
        # Real gap found live: an UNVERIFIABLE verdict (verification-engine
        # unreachable, or no confident HEALTHY/FAILED call could be made in
        # the allotted window) has no real action to request — asking OPA
        # "can I PROMOTE_STEP?" for it is a category error, and the
        # policy's own "promotion requires HEALTHY" rule would reject it as
        # a generic BLOCKED action anyway, so this never reached the
        # status-based branches below at all. alert_verification_timeout
        # existed in alert_dispatcher.py since Phase 6 but was never once
        # called anywhere — the assignment explicitly requires notifying
        # someone "the moment ... verification can't reach a confident
        # verdict in the allotted time". Deliberately no actuation here —
        # never auto-promote OR auto-rollback on inconclusive evidence, only
        # notify; a human decides what happens next.
        collected, required = _extract_sample_counts(verdict, policy)
        await alert_verification_timeout(pipeline_run_id, samples_collected=collected, samples_required=required)
        logger.warning(
            "verdict_unverifiable",
            pipeline_run_id=pipeline_run_id,
            samples_collected=collected,
            samples_required=required,
            note=verdict.get("note"),
        )
        return

    # Resolved up front (not just before actuation) because RULE 7 in
    # policies/delivery_guardrails.rego needs a REAL cost_analysis.delta_percent
    # to gate PROMOTE_STEP on — a hardcoded 0.0 here meant that guardrail could
    # never fire regardless of how much a canary rollout actually cost (see
    # cost_tracker.py's module docstring). compute_and_record_cost reads the
    # live baseline/canary Deployments' actual replica+resource footprint and
    # is fail-soft (returns None, never raises) so a demo/no-cluster pipeline
    # still runs — it just never gets cost-gated or a real cost_analysis row.
    target = await _get_actuation_target(redis_client, pipeline_run_id)
    cost_result = await compute_and_record_cost(
        pipeline_run_id=pipeline_run_id,
        tenant_id=target.get("tenant_id"),
        namespace=target["namespace"],
        baseline_deployment_name=target["baseline_deployment_name"],
        canary_deployment_name=target["canary_deployment_name"],
        db=db,
        max_permitted_delta_percent=policy.get("guardrails", {}).get("maxPermittedCostDeltaPercent", 15.0),
    )

    promotion_fields, active_step = _build_promotion_opa_input(
        verdict, policy, rollout_state, cost_result["delta_percent"] if cost_result else 0.0, approved_signatures=[]
    )
    opa_input = {
        "requested_action": "ROLLBACK" if verdict.get("status") == "FAILED" else "PROMOTE_STEP",
        "verification_verdict": verdict,
        "pipeline_policy": policy,
        "runtime_context": {"cluster_maintenance_lock": False},
        **promotion_fields,
    }

    opa_result = await evaluate_policy_async(opa_input)  # Structural gate #2 (OPA)
    is_rollback = verdict.get("status") == "FAILED"
    failsafe_rollback_override = (
        not opa_result["allow_action"] and opa_result.get("opa_unreachable") and is_rollback
    )

    if not opa_result["allow_action"] and not failsafe_rollback_override:
        if verdict.get("status") == "HEALTHY" and active_step and active_step.get("requiresManualApproval"):
            # Real gap found live: a step flagged requiresManualApproval in
            # the pipeline's own declared schedule (the final 100% cutover,
            # by default) always fell into the generic BLOCKED alert below
            # with no way for a human to ever actually unblock it —
            # approved_signatures had nothing real behind it anywhere in
            # this codebase (policy_router.py's default OPA input hardcodes
            # it empty too). alert_approval_required existed since Phase 6
            # but was never called. Persisting the verdict that already
            # proved HEALTHY here means a real approval (handle_approval,
            # below) can re-evaluate this exact decision instead of
            # demanding a brand new verification cycle for evidence that's
            # already in hand.
            rollout_state["status"] = "AWAITING_APPROVAL"
            rollout_state["pending_verdict"] = verdict
            rollout_state["pending_policy"] = policy
            await rollout_scheduler.save_rollout_state(redis_client, pipeline_run_id, rollout_state)
            roles = policy.get("gates", {}).get("manualApprovalRequired", {}).get("approverRoles", [])
            await alert_approval_required(pipeline_run_id, stage=opa_input["target_stage"], roles=roles)
            logger.info(
                "promotion_awaiting_approval", pipeline_run_id=pipeline_run_id, stage=opa_input["target_stage"]
            )
            return
        await send_alert("BLOCKED", f"Action blocked: {opa_result.get('rejection_reasons')}")
        return

    if failsafe_rollback_override:
        logger.warning(
            "opa_unreachable_failsafe_rollback_override",
            pipeline_run_id=pipeline_run_id,
            reason="OPA unreachable during a FAILED verdict — rolling back anyway rather than "
            "leaving a known-bad canary serving traffic because the guardrail check itself is down.",
        )
        await send_alert(
            "SECURITY", f"OPA unreachable — rolling back {pipeline_run_id} via fail-safe override, not blocking it"
        )

    if verdict.get("status") == "FAILED":
        await emergency_rollback(
            pipeline_run_id,
            authorized_by="FAILSAFE:opa_unreachable" if failsafe_rollback_override else "OPA:rule=ROLLBACK",
            route_name=target["route_name"],
            namespace=target["namespace"],
            canary_deployment_name=target["canary_deployment_name"],
            tenant_id=target.get("tenant_id"),
            db=db,
            verdict_status=verdict.get("status"),
            confidence=verdict.get("confidence"),
        )
        await alert_rollback(pipeline_run_id, reason="Statistical failure detected by verification engine")
        await trigger_rca_async(db, tenant_id=target.get("tenant_id"), verdict=verdict, action="ROLLBACK")
        if rollout_state is not None:
            rollout_state["status"] = "ROLLED_BACK"
            await rollout_scheduler.save_rollout_state(redis_client, pipeline_run_id, rollout_state)
    elif verdict.get("status") == "HEALTHY":
        # Real per-step weight when the pipeline declared a real ramp
        # (10% -> 25% -> 50% -> 100%, each independently verified) —
        # falls back to the old flat-25% default only for a legacy run
        # with no registered rollout_state (see _build_promotion_opa_input).
        canary_weight = active_step["trafficWeight"] if active_step else verdict.get("recommended_weight", 25)
        baseline_weight = 100 - canary_weight
        await update_traffic_weights(
            pipeline_run_id,
            canary_weight=canary_weight,
            baseline_weight=baseline_weight,
            authorized_by="OPA:rule=PROMOTE_STEP",
            route_name=target["route_name"],
            namespace=target["namespace"],
            tenant_id=target.get("tenant_id"),
            db=db,
            verdict_status=verdict.get("status"),
            confidence=verdict.get("confidence"),
        )
        await trigger_rca_async(db, tenant_id=target.get("tenant_id"), verdict=verdict, action="PROMOTE_STEP")
        await _advance_or_graduate(pipeline_run_id, target, rollout_state, redis_client, trace_id=None, db=db)


async def _advance_or_graduate(
    pipeline_run_id: str,
    target: dict,
    rollout_state: dict | None,
    redis_client,
    trace_id: str | None,
    db=None,
) -> None:
    """
    After a successful PROMOTE_STEP actuation: continues the automated ramp
    to the next declared step, leaves the run alone if the next step
    requires manual approval (handle_incoming_verdict's own HEALTHY-verdict
    pass at that step is what actually pauses and alerts — this only avoids
    skipping past the gate), or — once the final step is reached —
    graduates the canary into the new baseline so the NEXT rollout starts
    from what was just proven healthy instead of comparing against a
    permanently stale version forever.
    """
    if rollout_state is None:
        return  # legacy/no-schedule run — nothing further to automate

    steps = rollout_state["steps"]
    next_index = rollout_state["current_step_index"] + 1

    if next_index >= len(steps):
        status = await rollout_scheduler.graduate(pipeline_run_id, target, rollout_state.get("target_version"), db=db)
        rollout_state["status"] = status
        await rollout_scheduler.save_rollout_state(redis_client, pipeline_run_id, rollout_state)
        return

    if steps[next_index].get("requiresManualApproval"):
        return

    await rollout_scheduler.advance_to_next_step(
        redis_client, pipeline_run_id, target.get("tenant_id"), rollout_state, next_index, trace_id
    )


async def handle_approval(
    pipeline_run_id: str, approver_role: str, approver_user: str | None, redis_client, db=None
) -> dict:
    """
    Real gap found live: `alert_approval_required` fired (once wired up
    above) but there was no way for a human to actually UNBLOCK a paused
    promotion anywhere in this codebase — `approved_signatures` was
    permanently `[]` (policy_router.py's default OPA input hardcodes it
    empty too). Re-evaluates the SAME already-HEALTHY verdict that
    triggered the pause, this time with a real approval signature, rather
    than demanding a brand new verification cycle for evidence that
    already exists — and, if OPA now allows it, actuates the final
    promotion and continues (or completes) the ramp exactly like a normal
    autonomous promotion would.
    """
    rollout_state = await rollout_scheduler.get_rollout_state(redis_client, pipeline_run_id)
    if rollout_state is None or rollout_state.get("status") != "AWAITING_APPROVAL":
        return {"status": "NO_PENDING_APPROVAL"}

    verdict = rollout_state["pending_verdict"]
    policy = rollout_state["pending_policy"]
    target = await _get_actuation_target(redis_client, pipeline_run_id)
    approved_signatures = [{"role": approver_role, "user": approver_user}]

    cost_result = await compute_and_record_cost(
        pipeline_run_id=pipeline_run_id,
        tenant_id=target.get("tenant_id"),
        namespace=target["namespace"],
        baseline_deployment_name=target["baseline_deployment_name"],
        canary_deployment_name=target["canary_deployment_name"],
        db=db,
        max_permitted_delta_percent=policy.get("guardrails", {}).get("maxPermittedCostDeltaPercent", 15.0),
    )
    promotion_fields, active_step = _build_promotion_opa_input(
        verdict, policy, rollout_state, cost_result["delta_percent"] if cost_result else 0.0, approved_signatures
    )
    opa_input = {
        "requested_action": "PROMOTE_STEP",
        "verification_verdict": verdict,
        "pipeline_policy": policy,
        "runtime_context": {"cluster_maintenance_lock": False},
        **promotion_fields,
    }
    opa_result = await evaluate_policy_async(opa_input)
    if not opa_result["allow_action"]:
        await send_alert("BLOCKED", f"Approved promotion still blocked: {opa_result.get('rejection_reasons')}")
        return {"status": "STILL_BLOCKED", "reasons": opa_result.get("rejection_reasons")}

    canary_weight = active_step["trafficWeight"] if active_step else 100
    baseline_weight = 100 - canary_weight
    await update_traffic_weights(
        pipeline_run_id,
        canary_weight=canary_weight,
        baseline_weight=baseline_weight,
        authorized_by=f"APPROVED:{approver_role}",
        route_name=target["route_name"],
        namespace=target["namespace"],
        tenant_id=target.get("tenant_id"),
        db=db,
        verdict_status=verdict.get("status"),
        confidence=verdict.get("confidence"),
    )
    await trigger_rca_async(db, tenant_id=target.get("tenant_id"), verdict=verdict, action="PROMOTE_STEP")
    await _advance_or_graduate(pipeline_run_id, target, rollout_state, redis_client, trace_id=None, db=db)
    return {"status": "PROMOTED", "canary_weight": canary_weight}


STREAM_VERDICTS = "stream:verdicts"
GROUP_POLICY_CONTROLLERS = "policy_controllers"
MAX_DELIVERY_ATTEMPTS = 3
STALE_CLAIM_MIN_IDLE_MS = 30_000
RECLAIM_INTERVAL_SECONDS = 60


async def _handle_stream_payload(redis_client, entry: dict, db=None) -> None:
    pipeline_run_id = entry["pipeline_run_id"]
    raw_message = json.dumps({"verdict": entry["verdict"], "signature": entry["signature"]})
    # Phase 6 (§06-observability-platform-ops.md, 6.1): the same trace_id
    # verification-engine bound for the /verify call that produced this
    # verdict — binding it here means this service's log lines for
    # processing THIS verdict correlate with the rest of the run's logs.
    bind_request_context(trace_id=entry.get("trace_id") or pipeline_run_id)
    try:
        await handle_incoming_verdict(raw_message, pipeline_run_id, redis_client=redis_client, db=db)
    finally:
        clear_request_context()


async def _reclaim_stale_pending_loop(redis_client, consumer_name: str, db=None):
    """
    Recovers verdicts left pending by a policy-controller replica that died
    before acking (crashed mid-actuation) — see pipeline-worker/src/main.py
    for the identical pattern on the other side of this phase's queue work.
    """
    while True:
        await asyncio.sleep(RECLAIM_INTERVAL_SECONDS)
        try:
            claimed = await streams.claim_stale_pending(
                redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS, consumer_name,
                min_idle_ms=STALE_CLAIM_MIN_IDLE_MS,
            )
            for message_id, entry in claimed:
                attempts = await streams.get_delivery_count(
                    redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS, message_id
                )
                if attempts > MAX_DELIVERY_ATTEMPTS:
                    logger.error(
                        "verdict_dead_lettered",
                        message_id=message_id,
                        pipeline_run_id=entry.get("pipeline_run_id"),
                        attempts=attempts,
                    )
                    await streams.ack(redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS, message_id)
                    continue
                logger.warning(
                    "verdict_retrying_stale_message",
                    message_id=message_id,
                    pipeline_run_id=entry.get("pipeline_run_id"),
                    attempts=attempts,
                )
                try:
                    await _handle_stream_payload(redis_client, entry, db=db)
                    await streams.ack(redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS, message_id)
                except Exception as e:
                    logger.error("verdict_retry_failed", message_id=message_id, error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("reclaim_stale_pending_loop_failed", error=str(e))


async def run_policy_controller_loop(db=None):
    """
    Reads verdicts from a Redis Stream consumer group — exactly one
    policy-controller replica processes each verdict (Phase 5,
    §05-reliability-scale.md deliverable 5.1). This used to be
    `psubscribe("verdicts:*")`, which delivers every verdict to EVERY
    subscribing replica; running 2+ replicas meant each independently
    actuated the same verdict.

    `db` (Phase 6 fix): a connected `src.db.PolicyControllerDB`, threaded
    through to every actuation so `audit_ledger` actually gets written —
    see db.py's module docstring for why this was missing since day one.
    """
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    consumer_name = socket.gethostname()
    await streams.ensure_group(redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS)
    logger.info("policy_controller_listening", stream=STREAM_VERDICTS, consumer=consumer_name)

    reclaim_task = asyncio.create_task(_reclaim_stale_pending_loop(redis_client, consumer_name, db=db))
    try:
        while True:
            # Phase 5 (§05-reliability-scale.md, deliverable 5.4): found live,
            # by actually stopping Redis — `read_one` itself must be inside
            # this try/except, not just the processing below it. The first
            # version only wrapped `_handle_stream_payload`/`ack`; a Redis
            # error from `read_one` during an outage propagated straight
            # through this `while True` and out of the function entirely,
            # silently killing the whole background task with no crash log
            # anywhere — verdicts stopped being processed forever, even
            # after Redis came back, because nothing ever retried. Matches
            # pipeline-worker's `_consume_pipeline_start`, which already had
            # this right.
            try:
                result = await streams.read_one(
                    redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS, consumer_name, block_ms=5000
                )
                if result is None:
                    continue
                message_id, entry = result
                try:
                    await _handle_stream_payload(redis_client, entry, db=db)
                    await streams.ack(redis_client, STREAM_VERDICTS, GROUP_POLICY_CONTROLLERS, message_id)
                except Exception as e:
                    logger.error(
                        "verdict_processing_failed", message_id=message_id, error=str(e), consumer=consumer_name
                    )
                    # Not acked — stays pending for _reclaim_stale_pending_loop.
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("verdict_consumer_failed", error=str(e), consumer=consumer_name)
                await asyncio.sleep(1)
    finally:
        reclaim_task.cancel()
        try:
            await reclaim_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(run_policy_controller_loop())
