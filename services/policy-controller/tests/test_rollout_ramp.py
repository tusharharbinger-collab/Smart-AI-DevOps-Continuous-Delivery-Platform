"""
services/policy-controller/tests/test_rollout_ramp.py

Real gap found live: a HEALTHY verdict always promoted the canary to a
hardcoded 25% traffic weight and the pipeline was marked COMPLETED after
that one verification cycle, regardless of how many steps a pipeline's
`canary_loop.steps` (10% -> 25% -> 50% -> 100%, each with its own real
minSampleSize/minDuration) actually declared — there was no automated
ramp, no real per-step gating, and no way to complete a promotion paused
at a `requiresManualApproval` step. These tests cover the real fix:
`rollout_state` in Redis drives the real step weight and real per-step
sample/duration figures fed to OPA, a successful promotion schedules the
next step (or pauses for approval, or graduates), and a human approval
re-evaluates the already-HEALTHY verdict instead of demanding a new one.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from src import controller
from src.verdict_verifier import get_signing_key


def _signed_healthy_payload(run_id: str) -> str:
    verdict = {
        "verdict_id": f"v-{run_id}",
        "pipeline_run_id": run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "HEALTHY",
        "confidence": 0.95,
        "evidence": {"error_rate": {"total_requests": 500}},
    }
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(get_signing_key(), canonical.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"verdict": canonical, "signature": sig})


def _steps(*, final_requires_approval=True):
    return [
        {"trafficWeight": 10, "minDurationSeconds": 60, "minSampleSize": 50, "requiresManualApproval": False},
        {"trafficWeight": 50, "minDurationSeconds": 120, "minSampleSize": 100, "requiresManualApproval": False},
        {"trafficWeight": 100, "minDurationSeconds": 0, "minSampleSize": 0, "requiresManualApproval": final_requires_approval},
    ]


class FakeRedis:
    """In-memory stand-in for the subset of the async Redis API rollout_scheduler uses."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture(autouse=True)
def _patch_actuation(monkeypatch):
    calls = {"promote": [], "rollback": [], "approval_alerts": [], "graduate": [], "advance": []}

    async def fake_promote(pipeline_run_id, canary_weight, baseline_weight, authorized_by, **kwargs):
        calls["promote"].append({"canary_weight": canary_weight, "authorized_by": authorized_by})

    async def fake_rollback(pipeline_run_id, authorized_by, **kwargs):
        calls["rollback"].append({"authorized_by": authorized_by})

    async def fake_send_alert(*args, **kwargs):
        pass

    async def fake_alert_approval_required(pipeline_run_id, stage, roles):
        calls["approval_alerts"].append({"pipeline_run_id": pipeline_run_id, "stage": stage, "roles": roles})

    async def fake_get_target(redis_client, pipeline_run_id):
        return {
            "route_name": "svc-route",
            "namespace": "production",
            "canary_deployment_name": "svc-canary",
            "baseline_deployment_name": "svc-baseline",
            "tenant_id": "tenant-1",
        }

    async def fake_opa_allow_promote(payload):
        """
        Mirrors just enough of RULE 2 + RULE 4 (delivery_guardrails.rego) for
        these tests: ROLLBACK always allowed; PROMOTE_STEP allowed unless the
        target stage is the final 100% cutover with no approval signature yet.
        """
        if payload["requested_action"] == "ROLLBACK":
            allow = True
        else:
            requires_approval = payload["target_stage"] == "step_100_promotion"
            allow = not requires_approval or len(payload.get("approved_signatures", [])) > 0
        return {
            "allow_action": allow,
            "require_human_approval": not allow,
            "rejection_reasons": [] if allow else ["manual approval required"],
            "raw_result": {},
            "opa_unreachable": False,
        }

    async def fake_cost(**kwargs):
        return None

    async def fake_graduate(run_id, target, new_version, db=None):
        calls["graduate"].append({"run_id": run_id, "tenant_id": target.get("tenant_id"), "new_version": new_version})
        return "GRADUATED"

    async def fake_advance(redis_client, run_id, tenant_id, state, next_index, trace_id):
        calls["advance"].append({"run_id": run_id, "next_index": next_index})
        state["current_step_index"] = next_index
        await controller.rollout_scheduler.save_rollout_state(redis_client, run_id, state)

    monkeypatch.setattr(controller, "update_traffic_weights", fake_promote)
    monkeypatch.setattr(controller, "emergency_rollback", fake_rollback)
    monkeypatch.setattr(controller, "send_alert", fake_send_alert)
    monkeypatch.setattr(controller, "alert_approval_required", fake_alert_approval_required)
    monkeypatch.setattr(controller, "_get_actuation_target", fake_get_target)
    monkeypatch.setattr(controller, "evaluate_policy_async", fake_opa_allow_promote)
    monkeypatch.setattr(controller, "compute_and_record_cost", fake_cost)
    monkeypatch.setattr(controller.rollout_scheduler, "graduate", fake_graduate)
    monkeypatch.setattr(controller.rollout_scheduler, "advance_to_next_step", fake_advance)
    return calls


def _make_rollout_state(steps, current_index=0):
    return {
        "steps": steps,
        "current_step_index": current_index,
        "step_started_at": (datetime.now(timezone.utc) - timedelta(seconds=999)).isoformat(),
        "verification_config": {},
        "status": "RUNNING",
        "target_version": "v1.2.0",
    }


def test_healthy_verdict_promotes_to_the_real_step_weight_not_a_flat_default(_patch_actuation):
    redis_client = FakeRedis()
    run_id = "run-ramp-1"
    state = _make_rollout_state(_steps(), current_index=0)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert len(_patch_actuation["promote"]) == 1
    assert _patch_actuation["promote"][0]["canary_weight"] == 10, "must use steps[0]'s real weight, not a flat 25"


def test_successful_non_final_step_schedules_the_next_step(_patch_actuation):
    redis_client = FakeRedis()
    run_id = "run-ramp-2"
    state = _make_rollout_state(_steps(), current_index=0)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert len(_patch_actuation["advance"]) == 1
    assert _patch_actuation["advance"][0]["next_index"] == 1
    assert len(_patch_actuation["graduate"]) == 0


def test_step_before_final_advances_into_the_approval_gated_step(_patch_actuation):
    """
    Real bug found live: this used to assert NO advance happened here, on
    the theory that reaching an approval-gated step should stop the ramp
    immediately. In reality, nothing else ever schedules that step's own
    verification — skipping the advance meant the ramp silently dead-
    ended one step short of ever reaching the gate at all. Advancing the
    index only schedules the NEXT verification cycle; it does not itself
    promote or skip the human-approval check, which still fires correctly
    once a verdict comes back for that step (see the next test).
    """
    redis_client = FakeRedis()
    run_id = "run-ramp-3"
    state = _make_rollout_state(_steps(), current_index=1)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert _patch_actuation["promote"][0]["canary_weight"] == 50
    assert len(_patch_actuation["advance"]) == 1, "must schedule the approval-gated step's own verification"
    assert _patch_actuation["advance"][0]["next_index"] == 2
    assert len(_patch_actuation["graduate"]) == 0


def test_final_step_without_approval_signature_pauses_and_alerts(_patch_actuation):
    redis_client = FakeRedis()
    run_id = "run-ramp-4"
    state = _make_rollout_state(_steps(), current_index=2)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert len(_patch_actuation["promote"]) == 0, "must not promote the final step without a real approval"
    assert len(_patch_actuation["approval_alerts"]) == 1
    assert _patch_actuation["approval_alerts"][0]["stage"] == "step_100_promotion"

    saved = asyncio.run(controller.rollout_scheduler.get_rollout_state(redis_client, run_id))
    assert saved["status"] == "AWAITING_APPROVAL"
    assert saved["pending_verdict"]["status"] == "HEALTHY"


def test_approval_promotes_the_final_step_and_graduates(_patch_actuation):
    redis_client = FakeRedis()
    run_id = "run-ramp-5"
    state = _make_rollout_state(_steps(), current_index=2)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    # First: the pause (mirrors what a real HEALTHY verdict at the final step does).
    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))
    assert len(_patch_actuation["promote"]) == 0

    # Then: a human approves — no new verification cycle, same verdict re-evaluated.
    result = asyncio.run(
        controller.handle_approval(run_id, approver_role="lead-sre", approver_user="sre@acme.test", redis_client=redis_client)
    )

    assert result["status"] == "PROMOTED"
    assert len(_patch_actuation["promote"]) == 1
    assert _patch_actuation["promote"][0]["canary_weight"] == 100
    assert _patch_actuation["promote"][0]["authorized_by"] == "APPROVED:lead-sre"
    assert len(_patch_actuation["graduate"]) == 1
    assert _patch_actuation["graduate"][0]["new_version"] == "v1.2.0"


def test_approval_with_no_pending_approval_is_a_clean_no_op(_patch_actuation):
    redis_client = FakeRedis()
    result = asyncio.run(
        controller.handle_approval("run-nothing-pending", approver_role="lead-sre", approver_user=None, redis_client=redis_client)
    )
    assert result["status"] == "NO_PENDING_APPROVAL"
    assert len(_patch_actuation["promote"]) == 0


def test_legacy_run_with_no_rollout_state_keeps_the_old_flat_default_behavior(_patch_actuation):
    """A run from before this feature existed (or triggered outside the
    project wizard) has no rollout_state — must not crash, and must fall
    back to exactly the old behavior (flat 25% default, no ramp)."""
    redis_client = FakeRedis()
    run_id = "run-legacy-no-state"

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert len(_patch_actuation["promote"]) == 1
    assert _patch_actuation["promote"][0]["canary_weight"] == 25
    assert len(_patch_actuation["advance"]) == 0
    assert len(_patch_actuation["graduate"]) == 0


def test_failed_verdict_marks_rollout_state_rolled_back(_patch_actuation):
    redis_client = FakeRedis()
    run_id = "run-ramp-fail"
    state = _make_rollout_state(_steps(), current_index=1)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    verdict = {
        "verdict_id": "v-fail",
        "pipeline_run_id": run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "FAILED",
        "confidence": 0.9,
        "evidence": {},
    }
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(get_signing_key(), canonical.encode(), hashlib.sha256).hexdigest()
    payload = json.dumps({"verdict": canonical, "signature": sig})

    asyncio.run(controller.handle_incoming_verdict(payload, run_id, redis_client=redis_client))

    assert len(_patch_actuation["rollback"]) == 1
    saved = asyncio.run(controller.rollout_scheduler.get_rollout_state(redis_client, run_id))
    assert saved["status"] == "ROLLED_BACK"


def test_pipeline_real_declared_policy_reaches_opa_not_a_hardcoded_default(monkeypatch, _patch_actuation):
    """
    Real gap found live: handle_incoming_verdict's `pipeline_policy`
    parameter was never actually supplied by its only real caller
    (_handle_stream_payload) — every verdict, for every pipeline, was
    evaluated against a generic hardcoded default regardless of what that
    pipeline's own YAML declared. Most damagingly, the default's
    `manualApprovalRequired.beforeStages` was always `[]`, so a step
    flagged `requiresManualApproval` could never actually be gated. This
    proves a pipeline's REAL declared policy (carried in rollout_state by
    worker.py) is what actually reaches OPA now.
    """
    redis_client = FakeRedis()
    run_id = "run-ramp-real-policy"
    state = _make_rollout_state(_steps(), current_index=0)
    real_policy = {
        "gates": {
            "blockedDeployWindows": [],
            "manualApprovalRequired": {"beforeStages": ["step_100_promotion"], "approverRoles": ["platform-admin"]},
        },
        "guardrails": {
            "autoRollbackOnVerdict": ["FAILED"],
            "requireMinimumConfidence": 0.93,
            "minSampleSize": 500,
            "maxPermittedCostDeltaPercent": 8.0,
        },
    }
    state["pipeline_policy"] = real_policy
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    captured = {}

    async def capturing_opa_eval(payload):
        captured.update(payload)
        return {"allow_action": True, "require_human_approval": False, "rejection_reasons": [], "raw_result": {}, "opa_unreachable": False}

    monkeypatch.setattr(controller, "evaluate_policy_async", capturing_opa_eval)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert captured["pipeline_policy"] == real_policy, "the pipeline's own declared policy must reach OPA, not a generic default"
    assert captured["pipeline_policy"]["guardrails"]["requireMinimumConfidence"] == 0.93


async def _opa_eval_enforcing_real_evidence_gates(payload):
    """
    Mirrors RULE 2's real evidence requirements (delivery_guardrails.rego)
    that _patch_actuation's own fake OPA deliberately ignores — needed here
    specifically to prove the retry-on-insufficient-evidence fix, which
    only matters when the gate it's retrying against is real.
    """
    step = payload["current_step"]
    allow = (
        payload["requested_action"] == "PROMOTE_STEP"
        and payload["active_step_duration_seconds"] >= int(step["minDuration"].rstrip("s"))
        and payload["active_step_sample_count"] >= step["minSampleSize"]
    )
    return {
        "allow_action": allow,
        "require_human_approval": False,
        "rejection_reasons": [] if allow else ["insufficient evidence"],
        "raw_result": {},
        "opa_unreachable": False,
    }


def test_verdict_produced_with_no_real_elapsed_time_retries_the_same_step_instead_of_dead_ending(monkeypatch):
    """
    Real gap found live: a pipeline with no separate deploy/wait stage
    produces its FIRST verdict at essentially zero real elapsed time. A
    real minSampleSize/minDuration gate correctly rejects that — before
    this fix, the rejection fell into the generic BLOCKED alert and the
    run simply ended, never completing even its first step.
    """
    redis_client = FakeRedis()
    run_id = "run-ramp-fresh-step"
    state = _make_rollout_state(_steps(), current_index=0)
    state["step_started_at"] = datetime.now(timezone.utc).isoformat()  # just registered, ~0s elapsed
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    async def fake_get_target(redis_client, pipeline_run_id):
        return {
            "route_name": "svc-route", "namespace": "production",
            "canary_deployment_name": "svc-canary", "baseline_deployment_name": "svc-baseline",
            "tenant_id": "tenant-1",
        }

    calls = {"promote": [], "retry": [], "alerts": []}

    async def fake_promote(*args, **kwargs):
        calls["promote"].append(kwargs)

    async def fake_send_alert(*args, **kwargs):
        calls["alerts"].append(args)

    async def fake_retry(run_id, tenant_id, state, remaining_seconds, trace_id=None):
        calls["retry"].append({"run_id": run_id, "remaining_seconds": remaining_seconds})

    async def fake_cost(**kwargs):
        return None

    monkeypatch.setattr(controller, "_get_actuation_target", fake_get_target)
    monkeypatch.setattr(controller, "update_traffic_weights", fake_promote)
    monkeypatch.setattr(controller, "send_alert", fake_send_alert)
    monkeypatch.setattr(controller, "compute_and_record_cost", fake_cost)
    monkeypatch.setattr(controller, "evaluate_policy_async", _opa_eval_enforcing_real_evidence_gates)
    monkeypatch.setattr(controller.rollout_scheduler, "schedule_retry_of_current_step", fake_retry)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert len(calls["promote"]) == 0, "must not promote before the step's own evidence gate is genuinely satisfied"
    assert len(calls["alerts"]) == 0, "insufficient evidence isn't an alert-worthy failure, just not ready yet"
    assert len(calls["retry"]) == 1
    assert calls["retry"][0]["remaining_seconds"] > 0

    saved = asyncio.run(controller.rollout_scheduler.get_rollout_state(redis_client, run_id))
    assert saved["current_step_index"] == 0, "a same-step retry must never advance the step index"


def test_verdict_with_sufficient_evidence_is_not_retried_and_promotes_normally(monkeypatch):
    """Once real elapsed time/samples genuinely satisfy the step's own gate,
    this path must get out of the way and let the normal promotion happen."""
    redis_client = FakeRedis()
    run_id = "run-ramp-sufficient-evidence"
    state = _make_rollout_state(_steps(), current_index=0)  # step_started_at is 999s in the past (see helper)
    asyncio.run(redis_client.set(f"rollout_state:{run_id}", json.dumps(state)))

    async def fake_get_target(redis_client, pipeline_run_id):
        return {
            "route_name": "svc-route", "namespace": "production",
            "canary_deployment_name": "svc-canary", "baseline_deployment_name": "svc-baseline",
            "tenant_id": "tenant-1",
        }

    calls = {"promote": [], "retry": []}

    async def fake_promote(*args, **kwargs):
        calls["promote"].append(kwargs)

    async def fake_retry(*args, **kwargs):
        calls["retry"].append(kwargs)

    async def fake_cost(**kwargs):
        return None

    monkeypatch.setattr(controller, "_get_actuation_target", fake_get_target)
    monkeypatch.setattr(controller, "update_traffic_weights", fake_promote)
    monkeypatch.setattr(controller, "compute_and_record_cost", fake_cost)
    monkeypatch.setattr(controller, "evaluate_policy_async", _opa_eval_enforcing_real_evidence_gates)
    monkeypatch.setattr(controller.rollout_scheduler, "schedule_retry_of_current_step", fake_retry)
    monkeypatch.setattr(controller.rollout_scheduler, "advance_to_next_step", fake_promote)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(run_id), run_id, redis_client=redis_client))

    assert len(calls["retry"]) == 0
    assert len(calls["promote"]) >= 1
