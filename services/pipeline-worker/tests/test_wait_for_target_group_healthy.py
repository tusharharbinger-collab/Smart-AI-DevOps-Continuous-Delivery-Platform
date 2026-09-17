"""
services/pipeline-worker/tests/test_wait_for_target_group_healthy.py

Guaranteed Live Web App CI/CD (P0, blue-green) — real gap this closes: the
previous blue-green wiring only ever checked ECS task stability
(runningCount == desiredCount), which a crashing-after-startup app or a
misconfigured health_check_path can pass while still never becoming
routable. `wait_for_target_group_healthy` (shared/aws_ecs_actuation.py) is
the real ALB-driven HTTP check — the ALB itself performs the GET against
the target's health_check_path — used as the second half of blue-green's
pre-cutover gate. Fake boto3 client only, matching this repo's established
pattern for shared/aws_ecs_actuation.py's other functions
(test_ecs_onboarding.py) — no real AWS calls.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from shared import aws_ecs_actuation


class _FakeTargetGroupNotFound(Exception):
    pass


class _FakeElbv2:
    def __init__(self, health_sequence):
        # Each call to describe_target_health pops the next state off the
        # front — lets a test simulate "unhealthy, then healthy" polling.
        self._health_sequence = list(health_sequence)
        self.exceptions = type("Exceptions", (), {"TargetGroupNotFoundException": _FakeTargetGroupNotFound})()

    def describe_target_groups(self, Names):
        return {"TargetGroups": [{"TargetGroupArn": f"arn:aws:elasticloadbalancing:tg/{Names[0]}"}]}

    def describe_target_health(self, TargetGroupArn):
        states = self._health_sequence.pop(0) if len(self._health_sequence) > 1 else self._health_sequence[0]
        return {
            "TargetHealthDescriptions": [
                {"Target": {"Id": f"i-{idx}"}, "TargetHealth": {"State": state, "Reason": None}}
                for idx, state in enumerate(states)
            ]
        }


def test_returns_healthy_once_all_targets_report_healthy(monkeypatch):
    fake = _FakeElbv2(health_sequence=[["healthy"]])
    monkeypatch.setattr(aws_ecs_actuation.boto3, "client", lambda service, region_name: fake)
    monkeypatch.setattr(aws_ecs_actuation.time, "sleep", lambda s: None)

    result = aws_ecs_actuation.wait_for_target_group_healthy("us-east-1", "checkout-canary")
    assert result["healthy"] is True
    assert result["targets"][0]["state"] == "healthy"


def test_polls_until_all_targets_become_healthy(monkeypatch):
    fake = _FakeElbv2(health_sequence=[["unhealthy"], ["unhealthy"], ["healthy"]])
    monkeypatch.setattr(aws_ecs_actuation.boto3, "client", lambda service, region_name: fake)
    sleep_calls = []
    monkeypatch.setattr(aws_ecs_actuation.time, "sleep", lambda s: sleep_calls.append(s))

    result = aws_ecs_actuation.wait_for_target_group_healthy("us-east-1", "checkout-canary", poll_interval_seconds=0.01)
    assert result["healthy"] is True
    assert len(sleep_calls) == 2


def test_raises_on_timeout_without_ever_reporting_healthy(monkeypatch):
    fake = _FakeElbv2(health_sequence=[["unhealthy"]])
    monkeypatch.setattr(aws_ecs_actuation.boto3, "client", lambda service, region_name: fake)

    # Force the deadline to already be in the past on first check.
    monkeypatch.setattr(aws_ecs_actuation.time, "monotonic", lambda: 0)
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0 if calls["n"] == 1 else 999

    monkeypatch.setattr(aws_ecs_actuation.time, "monotonic", fake_monotonic)

    with pytest.raises(RuntimeError, match="did not report all targets healthy"):
        aws_ecs_actuation.wait_for_target_group_healthy("us-east-1", "checkout-canary", timeout_seconds=1)


def test_raises_a_clear_error_when_target_group_does_not_exist(monkeypatch):
    fake = _FakeElbv2(health_sequence=[["healthy"]])

    def raise_not_found(Names):
        raise fake.exceptions.TargetGroupNotFoundException()

    fake.describe_target_groups = raise_not_found
    monkeypatch.setattr(aws_ecs_actuation.boto3, "client", lambda service, region_name: fake)

    with pytest.raises(RuntimeError, match="not found"):
        aws_ecs_actuation.wait_for_target_group_healthy("us-east-1", "checkout-canary")
