"""
services/policy-controller/tests/test_aws_actuation_executor.py

Module 8 continuation — the real gap this closes: policy-controller's
ongoing weight-shift/graduate/rollback actuation was hardcoded to
Kubernetes with no way to touch a real AWS ECS project, so every commit
after a project's initial AWS onboarding had no way to ever actually reach
production traffic. Mirrors test_rollback_actuation.py's style exactly
(plain `asyncio.run`, no pytest-asyncio plugin in this service): feeds real
inputs, asserts the actual AWS mutation calls made (via the shared
`shared.aws_ecs_actuation` primitives, monkeypatched at the point this
module imports them), plus that a real audit record gets written. Never
touches real AWS.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from src import aws_actuation_executor


@pytest.fixture
def captured_audit_writes(monkeypatch):
    calls = []

    async def fake_record_actuation(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(aws_actuation_executor, "record_actuation", fake_record_actuation)
    return calls


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

    monkeypatch.setattr(aws_actuation_executor, "set_traffic_weights", fake_set_traffic_weights)
    return calls


def test_update_traffic_weights_ecs_shifts_weights_and_records_audit(captured_weight_calls, captured_audit_writes):
    result = asyncio.run(
        aws_actuation_executor.update_traffic_weights_ecs(
            "run-1", canary_weight=25, baseline_weight=75, authorized_by="OPA:rule=PROMOTE_STEP",
            service_name="checkout", path_prefix="/api/v1/checkout", region="us-east-1",
            tenant_id="tenant-1", verdict_status="HEALTHY", confidence=0.95,
        )
    )
    assert result == {"canary_weight": 25, "baseline_weight": 75}
    assert captured_weight_calls[0]["baseline_weight"] == 75 and captured_weight_calls[0]["canary_weight"] == 25
    assert captured_audit_writes[0]["action"] == "WEIGHT_UPDATE"
    assert captured_audit_writes[0]["tenant_id"] == "tenant-1"


def test_emergency_rollback_ecs_sets_100_0_and_scales_canary_to_zero(monkeypatch, captured_weight_calls, captured_audit_writes):
    scale_calls = []
    monkeypatch.setattr(
        aws_actuation_executor, "scale_service",
        lambda ecs, cluster, service, desired_count: scale_calls.append((cluster, service, desired_count)),
    )
    monkeypatch.setattr(aws_actuation_executor.boto3, "client", lambda service, region_name: object())

    result = asyncio.run(
        aws_actuation_executor.emergency_rollback_ecs(
            "run-1", authorized_by="OPA:rule=ROLLBACK", service_name="checkout",
            path_prefix="/api/v1/checkout", region="us-east-1", tenant_id="tenant-1",
        )
    )

    assert result == {"status": "ROLLED_BACK"}
    assert captured_weight_calls[0]["baseline_weight"] == 100 and captured_weight_calls[0]["canary_weight"] == 0
    assert scale_calls == [
        ("smartcd-platform", "checkout-baseline", 1),
        ("smartcd-platform", "checkout-canary", 0),
    ]
    assert captured_audit_writes[-1]["action"] == "ROLLBACK"


def test_emergency_rollback_ecs_still_records_audit_if_scale_down_fails(monkeypatch, captured_weight_calls, captured_audit_writes):
    """Mirrors actuation_executor.emergency_rollback's own tolerance: the
    real, safety-relevant change (traffic back to 0% canary) already
    happened — a scale-down failure on an already-drained-of-traffic
    service must not prevent the rollback from being recorded as done."""
    def _raise(*a, **k):
        raise RuntimeError("ECS API throttled")

    monkeypatch.setattr(aws_actuation_executor, "scale_service", _raise)
    monkeypatch.setattr(aws_actuation_executor.boto3, "client", lambda service, region_name: object())

    result = asyncio.run(
        aws_actuation_executor.emergency_rollback_ecs(
            "run-1", authorized_by="OPA:rule=ROLLBACK", service_name="checkout",
            path_prefix="/api/v1/checkout", region="us-east-1",
        )
    )
    assert result == {"status": "ROLLED_BACK"}
    assert captured_audit_writes[-1]["action"] == "ROLLBACK"


def test_blue_green_cutover_ecs_shifts_weights_atomically_and_records_audit(captured_weight_calls, captured_audit_writes):
    result = asyncio.run(
        aws_actuation_executor.blue_green_cutover_ecs(
            "run-1", authorized_by="OPA:rule=BLUE_GREEN_CUTOVER",
            service_name="checkout", path_prefix="/api/v1/checkout", region="us-east-1",
            tenant_id="tenant-1", verdict_status="HEALTHY", confidence=0.99,
        )
    )
    assert result == {"status": "CUTOVER"}
    assert captured_weight_calls[0]["baseline_weight"] == 0 and captured_weight_calls[0]["canary_weight"] == 100
    assert captured_audit_writes[0]["action"] == "BLUE_GREEN_CUTOVER"
    assert captured_audit_writes[0]["tenant_id"] == "tenant-1"


def test_graduate_canary_ecs_clones_canary_image_onto_baseline(monkeypatch, captured_weight_calls, captured_audit_writes):
    configs = {
        "checkout-canary": {
            "family": "checkout-canary", "image": "123.dkr.ecr.us-east-1.amazonaws.com/checkout:v1.1.0",
            "port": 8080, "cpu": "256", "memory": "512",
            "execution_role_arn": "arn:aws:iam::123:role/x", "log_group": "/ecs/checkout", "container_name": "checkout",
        },
        "checkout-baseline": {
            "family": "checkout-baseline", "image": "123.dkr.ecr.us-east-1.amazonaws.com/checkout:v1.0.0",
            "port": 8080, "cpu": "256", "memory": "512",
            "execution_role_arn": "arn:aws:iam::123:role/x", "log_group": "/ecs/checkout", "container_name": "checkout",
        },
    }
    registered = []
    ensure_service_calls = []
    scale_calls = []

    monkeypatch.setattr(
        aws_actuation_executor, "describe_current_container_config",
        lambda ecs, cluster, service_name: configs[service_name],
    )
    monkeypatch.setattr(
        aws_actuation_executor, "register_task_definition",
        lambda *a, **k: registered.append((a, k)) or "arn:aws:ecs:us-east-1:123:task-definition/checkout-baseline:2",
    )
    monkeypatch.setattr(
        aws_actuation_executor, "ensure_service",
        lambda *a, **k: ensure_service_calls.append((a, k)) or {"status": "updated"},
    )
    monkeypatch.setattr(
        aws_actuation_executor, "scale_service",
        lambda ecs, cluster, service, desired_count: scale_calls.append((service, desired_count)),
    )

    class _FakeElbv2:
        def describe_target_groups(self, Names):
            return {"TargetGroups": [{"TargetGroupArn": f"arn-{Names[0]}"}]}

    monkeypatch.setattr(
        aws_actuation_executor.boto3, "client",
        lambda service, region_name: _FakeElbv2() if service == "elbv2" else object(),
    )

    result = asyncio.run(
        aws_actuation_executor.graduate_canary_ecs(
            "run-1", authorized_by="SYSTEM:auto_graduate", service_name="checkout",
            path_prefix="/api/v1/checkout", region="us-east-1", tenant_id="tenant-1",
        )
    )

    assert result == {"status": "GRADUATED", "new_baseline_image": "123.dkr.ecr.us-east-1.amazonaws.com/checkout:v1.1.0"}
    # The image actually registered for baseline must be the canary's LIVE
    # image — never the originally-requested tag, in case it drifted.
    reg_args, reg_kwargs = registered[0]
    assert reg_args[2] == "123.dkr.ecr.us-east-1.amazonaws.com/checkout:v1.1.0"
    assert reg_kwargs.get("path_prefix") == "/api/v1/checkout"
    assert captured_weight_calls[0]["baseline_weight"] == 100 and captured_weight_calls[0]["canary_weight"] == 0
    assert scale_calls == [("checkout-canary", 0)]
    assert captured_audit_writes[-1]["action"] == "GRADUATE"


# ─────────────── Guaranteed Live Web App CI/CD — live-URL verification ───────────────


def _graduate_test_setup(monkeypatch):
    configs = {
        "checkout-canary": {
            "family": "checkout-canary", "image": "123.dkr.ecr.us-east-1.amazonaws.com/checkout:v1.1.0",
            "port": 8080, "cpu": "256", "memory": "512",
            "execution_role_arn": "arn:aws:iam::123:role/x", "log_group": "/ecs/checkout", "container_name": "checkout",
        },
        "checkout-baseline": {
            "family": "checkout-baseline", "image": "123.dkr.ecr.us-east-1.amazonaws.com/checkout:v1.0.0",
            "port": 8080, "cpu": "256", "memory": "512",
            "execution_role_arn": "arn:aws:iam::123:role/x", "log_group": "/ecs/checkout", "container_name": "checkout",
        },
    }
    monkeypatch.setattr(
        aws_actuation_executor, "describe_current_container_config",
        lambda ecs, cluster, service_name: configs[service_name],
    )
    monkeypatch.setattr(aws_actuation_executor, "register_task_definition", lambda *a, **k: "arn:...:2")
    monkeypatch.setattr(aws_actuation_executor, "ensure_service", lambda *a, **k: {"status": "updated"})
    monkeypatch.setattr(aws_actuation_executor, "scale_service", lambda *a, **k: None)

    class _FakeElbv2:
        def describe_target_groups(self, Names):
            return {"TargetGroups": [{"TargetGroupArn": f"arn-{Names[0]}"}]}

    monkeypatch.setattr(
        aws_actuation_executor.boto3, "client",
        lambda service, region_name: _FakeElbv2() if service == "elbv2" else object(),
    )


class _FakeDBWithLiveUrl:
    def __init__(self):
        self.calls = []

    async def record_live_url_verification(self, tenant_id, pipeline_run_id, verified):
        self.calls.append((tenant_id, pipeline_run_id, verified))


def test_graduate_verifies_the_real_live_url_when_alb_base_url_is_configured(
    monkeypatch, captured_weight_calls, captured_audit_writes
):
    _graduate_test_setup(monkeypatch)
    monkeypatch.setattr(aws_actuation_executor, "AWS_ALB_BASE_URL", "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com")
    verify_calls = []
    monkeypatch.setattr(
        aws_actuation_executor, "verify_live_url",
        lambda url: verify_calls.append(url) or {"verified": True, "status_code": 200, "error": None},
    )
    fake_db = _FakeDBWithLiveUrl()

    asyncio.run(
        aws_actuation_executor.graduate_canary_ecs(
            "run-1", authorized_by="SYSTEM:auto_graduate", service_name="checkout",
            path_prefix="/api/v1/checkout", region="us-east-1", tenant_id="tenant-1", db=fake_db,
        )
    )

    assert verify_calls == ["http://smartcd-platform-alb-123.us-east-1.elb.amazonaws.com/api/v1/checkout/"]
    assert fake_db.calls == [("tenant-1", "run-1", True)]


def test_graduate_failed_live_url_verification_is_recorded_but_never_fails_the_graduation(
    monkeypatch, captured_weight_calls, captured_audit_writes
):
    _graduate_test_setup(monkeypatch)
    monkeypatch.setattr(aws_actuation_executor, "AWS_ALB_BASE_URL", "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com")
    monkeypatch.setattr(
        aws_actuation_executor, "verify_live_url",
        lambda url: {"verified": False, "status_code": 404, "error": "HTTP 404"},
    )
    fake_db = _FakeDBWithLiveUrl()

    result = asyncio.run(
        aws_actuation_executor.graduate_canary_ecs(
            "run-1", authorized_by="SYSTEM:auto_graduate", service_name="checkout",
            path_prefix="/api/v1/checkout", region="us-east-1", tenant_id="tenant-1", db=fake_db,
        )
    )

    assert result["status"] == "GRADUATED"
    assert fake_db.calls == [("tenant-1", "run-1", False)]


def test_graduate_skips_live_url_check_when_no_alb_base_url_configured(
    monkeypatch, captured_weight_calls, captured_audit_writes
):
    _graduate_test_setup(monkeypatch)
    monkeypatch.setattr(aws_actuation_executor, "AWS_ALB_BASE_URL", None)
    verify_calls = []
    monkeypatch.setattr(aws_actuation_executor, "verify_live_url", lambda url: verify_calls.append(url))

    asyncio.run(
        aws_actuation_executor.graduate_canary_ecs(
            "run-1", authorized_by="SYSTEM:auto_graduate", service_name="checkout",
            path_prefix="/api/v1/checkout", region="us-east-1", tenant_id="tenant-1",
        )
    )
    assert verify_calls == []
