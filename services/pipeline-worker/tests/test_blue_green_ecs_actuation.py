"""
services/pipeline-worker/tests/test_blue_green_ecs_actuation.py

Guaranteed Live Web App CI/CD (P0) — real gap this closes: blue-green's
previously wired cutover (policy-controller's controller.py, OPA's
BLUE_GREEN_CUTOVER rule) only ever fired after a real statistical verdict
reached the final canary step, which a genuinely new web app with zero
real visitors can never produce. These three functions
(cutover_blue_green_ecs_weights, rollback_blue_green_ecs_weights,
graduate_blue_green_ecs) are the health-gated replacement, called directly
from worker.py's canary_loop blue-green branch — never through a
statistical verdict. Mirrors test_aws_actuation_executor.py's style
(policy-controller): monkeypatch the shared primitives at the point THIS
module imports them, assert the real sequence of AWS mutation calls. No
real AWS calls.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

import src.aws.ecs_deploy_task as ecs_deploy_task


@pytest.fixture
def captured_weight_calls(monkeypatch):
    calls = []

    def fake_set_traffic_weights(region, service_name, path_prefix, baseline_weight, canary_weight):
        calls.append(
            {
                "region": region, "service_name": service_name, "path_prefix": path_prefix,
                "baseline_weight": baseline_weight, "canary_weight": canary_weight,
            }
        )
        return {"status": "weights_updated"}

    monkeypatch.setattr(ecs_deploy_task, "set_traffic_weights", fake_set_traffic_weights)
    return calls


def _allow(monkeypatch):
    monkeypatch.setattr(ecs_deploy_task, "evaluate_policy_sync", lambda opa_input: {"allow_action": True})


def _deny(monkeypatch, reasons=None):
    monkeypatch.setattr(
        ecs_deploy_task, "evaluate_policy_sync",
        lambda opa_input: {"allow_action": False, "rejection_reasons": reasons or ["frozen"]},
    )


# ─────────────────────────── cutover_blue_green_ecs_weights ───────────────────────────


def test_cutover_requests_health_gated_cutover_action(monkeypatch, captured_weight_calls):
    captured_input = {}

    def fake_evaluate(opa_input):
        captured_input.update(opa_input)
        return {"allow_action": True}

    monkeypatch.setattr(ecs_deploy_task, "evaluate_policy_sync", fake_evaluate)

    ecs_deploy_task.cutover_blue_green_ecs_weights(
        "run-1", service_name="checkout", path_prefix="/api/v1/checkout",
        region="us-east-1", pipeline_policy={"gates": {"blockedDeployWindows": []}},
    )
    assert captured_input["requested_action"] == "HEALTH_GATED_CUTOVER"
    # Critically, no verification_verdict is ever threaded through — this
    # path is reached precisely because no real verdict exists yet.
    assert "verification_verdict" not in captured_input


def test_cutover_flips_to_100_canary_0_baseline_when_allowed(monkeypatch, captured_weight_calls):
    _allow(monkeypatch)
    ecs_deploy_task.cutover_blue_green_ecs_weights(
        "run-1", service_name="checkout", path_prefix="/api/v1/checkout",
        region="us-east-1", pipeline_policy={},
    )
    assert captured_weight_calls == [
        {
            "region": "us-east-1", "service_name": "checkout", "path_prefix": "/api/v1/checkout",
            "baseline_weight": 0, "canary_weight": 100,
        }
    ]


def test_cutover_raises_and_never_touches_weights_when_opa_denies(monkeypatch, captured_weight_calls):
    _deny(monkeypatch, reasons=["Current timestamp falls within an enterprise-blocked deployment window"])
    with pytest.raises(RuntimeError, match="blocked by policy"):
        ecs_deploy_task.cutover_blue_green_ecs_weights(
            "run-1", service_name="checkout", path_prefix="/api/v1/checkout",
            region="us-east-1", pipeline_policy={},
        )
    assert captured_weight_calls == []


# ─────────────────────────── rollback_blue_green_ecs_weights ───────────────────────────


def test_rollback_reverts_to_100_baseline_0_canary_with_no_opa_gate(monkeypatch, captured_weight_calls):
    # Deliberately no evaluate_policy_sync patch at all — a rollback to the
    # already-proven-healthy previous version must never be policy-blockable.
    ecs_deploy_task.rollback_blue_green_ecs_weights(
        "run-1", service_name="checkout", path_prefix="/api/v1/checkout", region="us-east-1",
    )
    assert captured_weight_calls == [
        {
            "region": "us-east-1", "service_name": "checkout", "path_prefix": "/api/v1/checkout",
            "baseline_weight": 100, "canary_weight": 0,
        }
    ]


# ─────────────────────────── graduate_blue_green_ecs ───────────────────────────


def test_graduate_registers_image_onto_baseline_then_restores_steady_state_and_idles_canary(
    monkeypatch, captured_weight_calls
):
    baseline_deploy_calls = []
    wait_calls = []
    scale_calls = []

    monkeypatch.setattr(
        ecs_deploy_task, "deploy_ecs_baseline_task",
        lambda run_id, service_name, image, image_tag, region: baseline_deploy_calls.append(
            (service_name, image, image_tag, region)
        ),
    )
    monkeypatch.setattr(
        ecs_deploy_task, "wait_for_ecs_service_ready",
        lambda region, service_name, cohort: wait_calls.append((service_name, cohort)),
    )
    monkeypatch.setattr(ecs_deploy_task, "_clients", lambda region: {"ecs": object()})
    monkeypatch.setattr(
        ecs_deploy_task, "scale_service",
        lambda ecs, cluster, service_name, desired_count: scale_calls.append((cluster, service_name, desired_count)),
    )

    ecs_deploy_task.graduate_blue_green_ecs(
        "run-1", service_name="checkout", image="123.dkr.ecr.us-east-1.amazonaws.com/checkout",
        image_tag="v1.2.0", path_prefix="/api/v1/checkout", region="us-east-1",
    )

    assert baseline_deploy_calls == [
        ("checkout", "123.dkr.ecr.us-east-1.amazonaws.com/checkout", "v1.2.0", "us-east-1")
    ]
    # Baseline must be confirmed stable on the new image BEFORE weights are
    # touched — same ordering discipline as the first-deployment/canary-ramp
    # paths already use.
    assert wait_calls == [("checkout", "baseline")]
    assert captured_weight_calls == [
        {
            "region": "us-east-1", "service_name": "checkout", "path_prefix": "/api/v1/checkout",
            "baseline_weight": 100, "canary_weight": 0,
        }
    ]
    assert scale_calls == [(ecs_deploy_task.CLUSTER, "checkout-canary", 0)]


def test_graduate_scale_down_failure_is_not_fatal(monkeypatch, captured_weight_calls):
    monkeypatch.setattr(
        ecs_deploy_task, "deploy_ecs_baseline_task", lambda *a, **k: None
    )
    monkeypatch.setattr(ecs_deploy_task, "wait_for_ecs_service_ready", lambda *a, **k: None)
    monkeypatch.setattr(ecs_deploy_task, "_clients", lambda region: {"ecs": object()})

    def raise_scale_error(ecs, cluster, service_name, desired_count):
        raise RuntimeError("simulated AWS throttling")

    monkeypatch.setattr(ecs_deploy_task, "scale_service", raise_scale_error)

    # Must not raise — the safety-relevant change (baseline on the new
    # image, traffic on a stable steady state) already succeeded by the
    # time scale-down is attempted.
    result = ecs_deploy_task.graduate_blue_green_ecs(
        "run-1", service_name="checkout", image="123.dkr.ecr.us-east-1.amazonaws.com/checkout",
        image_tag="v1.2.0", path_prefix="/api/v1/checkout", region="us-east-1",
    )
    assert result["status"] == "weights_updated"
