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
