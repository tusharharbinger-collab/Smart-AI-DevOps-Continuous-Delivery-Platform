"""
services/policy-controller/tests/test_cost_guardrail_wiring.py

Proves the fix end-to-end at the handle_incoming_verdict level (not just
cost_tracker.py's own unit tests): the real computed cost delta actually
reaches OPA's `input.cost_analysis.delta_percent`, and RULE 7
(cost_delta_exceeds_limit) can genuinely block a promotion it previously
never could, because controller.py hardcoded that field to 0.0.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from src import controller
from src.verdict_verifier import get_signing_key


def _signed_healthy_payload() -> str:
    verdict = {
        "verdict_id": "v-healthy-cost",
        "pipeline_run_id": "run-cost-wiring-test",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "HEALTHY",
        "confidence": 0.95,
        "evidence": {"error_rate": {"total_requests": 150}},
    }
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(get_signing_key(), canonical.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"verdict": canonical, "signature": sig})


@pytest.fixture(autouse=True)
def _patch_actuation(monkeypatch):
    calls = {"promote": [], "opa_inputs": []}

    async def fake_promote(pipeline_run_id, canary_weight, baseline_weight, authorized_by, **kwargs):
        calls["promote"].append({"pipeline_run_id": pipeline_run_id, "authorized_by": authorized_by})

    async def fake_send_alert(*args, **kwargs):
        pass

    async def fake_get_target(redis_client, pipeline_run_id):
        return {
            "route_name": "svc-route",
            "namespace": "production",
            "canary_deployment_name": "svc-canary",
            "baseline_deployment_name": "svc-baseline",
            "tenant_id": "tenant-1",
        }

    monkeypatch.setattr(controller, "update_traffic_weights", fake_promote)
    monkeypatch.setattr(controller, "send_alert", fake_send_alert)
    monkeypatch.setattr(controller, "_get_actuation_target", fake_get_target)
    return calls


def _opa_eval_mirroring_real_cost_rule(payload):
    """
    Stands in for a live OPA server (not available in a unit test) but
    mirrors RULE 7 (cost_delta_exceeds_limit) from
    policies/delivery_guardrails.rego exactly, so this test proves
    controller.py forwards a REAL, non-hardcoded delta_percent into
    opa_input — the actual bug — without needing `opa` running.
    """
    delta = payload["cost_analysis"]["delta_percent"]
    ceiling = payload["pipeline_policy"]["guardrails"]["maxPermittedCostDeltaPercent"]
    cost_exceeds = delta > ceiling
    allow = payload["requested_action"] == "PROMOTE_STEP" and not cost_exceeds
    return {
        "allow_action": allow,
        "require_human_approval": False,
        "rejection_reasons": [] if allow else [f"Cost delta {delta}% exceeds the permitted maximum of {ceiling}%"],
        "raw_result": {},
        "opa_unreachable": False,
    }


def test_real_cost_delta_reaches_opa_and_blocks_an_expensive_promotion(monkeypatch, _patch_actuation):
    """Before this fix, cost_analysis.delta_percent was hardcoded to 0.0 in
    controller.py, so this guardrail could never fire regardless of real cost."""

    async def fake_expensive_cost(**kwargs):
        return {"delta_percent": 40.0, "exceeds_policy_limit": True}

    monkeypatch.setattr(controller, "compute_and_record_cost", fake_expensive_cost)

    captured_opa_input = {}

    async def capturing_opa_eval(payload):
        captured_opa_input.update(payload)
        return _opa_eval_mirroring_real_cost_rule(payload)

    monkeypatch.setattr(controller, "evaluate_policy_async", capturing_opa_eval)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert captured_opa_input["cost_analysis"]["delta_percent"] == 40.0
    assert len(_patch_actuation["promote"]) == 0, "a 40% cost overrun must block promotion under a 15% ceiling"


def test_cheap_promotion_still_proceeds_when_cost_is_within_budget(monkeypatch, _patch_actuation):
    async def fake_cheap_cost(**kwargs):
        return {"delta_percent": 2.0, "exceeds_policy_limit": False}

    monkeypatch.setattr(controller, "compute_and_record_cost", fake_cheap_cost)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert len(_patch_actuation["promote"]) == 1, "a promotion within the cost ceiling must still proceed"


def test_promotion_not_blocked_when_cost_tracker_is_fail_soft_none(monkeypatch, _patch_actuation):
    """cost_tracker.py returns None when the cluster is unreachable — this must
    fall back to delta_percent=0.0 (never blocking), not crash the whole verdict."""

    async def fake_unreachable(**kwargs):
        return None

    monkeypatch.setattr(controller, "compute_and_record_cost", fake_unreachable)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert len(_patch_actuation["promote"]) == 1


# ─── AWS ECS deploy-target wiring ──────────────────────────────────────
# Real gap this closes: before this fix, target.get("deployment_target")
# == "aws_ecs" meant cost_result was hardcoded to None (see the old
# handle_incoming_verdict comment this replaced) — an AWS project's
# RULE 7 guardrail could never fire no matter how expensive a canary
# actually was. These mirror the three Kubernetes wiring tests above,
# proving _compute_cost now genuinely branches onto compute_and_record_cost_ecs
# instead of compute_and_record_cost for an aws_ecs target.

@pytest.fixture
def _patch_actuation_aws_ecs(monkeypatch):
    calls = {"promote": [], "opa_inputs": []}

    async def fake_promote(pipeline_run_id, canary_weight, baseline_weight, authorized_by, **kwargs):
        calls["promote"].append({"pipeline_run_id": pipeline_run_id, "authorized_by": authorized_by})

    async def fake_send_alert(*args, **kwargs):
        pass

    async def fake_get_target(redis_client, pipeline_run_id):
        return {
            "route_name": "svc-route",
            "namespace": "production",
            "canary_deployment_name": "svc-canary",
            "baseline_deployment_name": "svc-baseline",
            "tenant_id": "tenant-1",
            "deployment_target": "aws_ecs",
            "aws_region": "us-east-1",
            "path_prefix": "/api/v1/svc",
        }

    monkeypatch.setattr(controller, "update_traffic_weights_ecs", fake_promote)
    monkeypatch.setattr(controller, "send_alert", fake_send_alert)
    monkeypatch.setattr(controller, "_get_actuation_target", fake_get_target)
    return calls


def test_real_ecs_cost_delta_reaches_opa_and_blocks_an_expensive_promotion(monkeypatch, _patch_actuation_aws_ecs):
    async def fake_expensive_ecs_cost(**kwargs):
        assert kwargs["baseline_service_name"] == "svc-baseline"
        assert kwargs["canary_service_name"] == "svc-canary"
        assert kwargs["region"] == "us-east-1"
        return {"delta_percent": 40.0, "exceeds_policy_limit": True}

    monkeypatch.setattr(controller, "compute_and_record_cost_ecs", fake_expensive_ecs_cost)

    captured_opa_input = {}

    async def capturing_opa_eval(payload):
        captured_opa_input.update(payload)
        return _opa_eval_mirroring_real_cost_rule(payload)

    monkeypatch.setattr(controller, "evaluate_policy_async", capturing_opa_eval)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert captured_opa_input["cost_analysis"]["delta_percent"] == 40.0
    assert len(_patch_actuation_aws_ecs["promote"]) == 0, "a 40% cost overrun must block promotion under a 15% ceiling on AWS too"


def test_cheap_ecs_promotion_still_proceeds_when_cost_is_within_budget(monkeypatch, _patch_actuation_aws_ecs):
    async def fake_cheap_ecs_cost(**kwargs):
        return {"delta_percent": 2.0, "exceeds_policy_limit": False}

    monkeypatch.setattr(controller, "compute_and_record_cost_ecs", fake_cheap_ecs_cost)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert len(_patch_actuation_aws_ecs["promote"]) == 1, "a promotion within the cost ceiling must still proceed on AWS too"


def test_ecs_promotion_not_blocked_when_cost_tracker_ecs_is_fail_soft_none(monkeypatch, _patch_actuation_aws_ecs):
    """compute_and_record_cost_ecs returns None when AWS is unreachable — this
    must fall back to delta_percent=0.0 (never blocking), not crash the verdict."""

    async def fake_unreachable(**kwargs):
        return None

    monkeypatch.setattr(controller, "compute_and_record_cost_ecs", fake_unreachable)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert len(_patch_actuation_aws_ecs["promote"]) == 1


def test_kubernetes_target_never_calls_the_ecs_cost_function(monkeypatch, _patch_actuation):
    """The branch in _compute_cost must be mutually exclusive — a plain
    Kubernetes target (the default _patch_actuation fixture, no
    deployment_target key) must never reach compute_and_record_cost_ecs."""

    async def exploding_ecs_cost(**kwargs):
        raise AssertionError("compute_and_record_cost_ecs must not be called for a kubernetes target")

    async def fake_k8s_cost(**kwargs):
        return {"delta_percent": 2.0, "exceeds_policy_limit": False}

    monkeypatch.setattr(controller, "compute_and_record_cost_ecs", exploding_ecs_cost)
    monkeypatch.setattr(controller, "compute_and_record_cost", fake_k8s_cost)

    asyncio.run(controller.handle_incoming_verdict(_signed_healthy_payload(), "run-cost-wiring-test"))

    assert len(_patch_actuation["promote"]) == 1
