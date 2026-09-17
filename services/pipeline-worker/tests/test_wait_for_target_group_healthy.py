"""
services/pipeline-worker/tests/test_wait_for_target_group_healthy.py

Guaranteed Live Web App CI/CD (P0, blue-green) — real gap this closes: the
previous blue-green wiring only ever checked ECS task stability
(runningCount == desiredCount), which a crashing-after-startup app or a
misconfigured health_check_path can pass while still never becoming
routable. `wait_for_target_group_healthy` (shared/aws_ecs_actuation.py) is
the real ALB-driven HTTP check — the ALB itself performs the GET against
the target's health_check_path — used as the second half of blue-green's
pre-cutover gate.

Real gap found live (2026-09-17), via a deliberate break-and-drill against
a real project: the original version required EVERY currently-registered
target to be healthy — including a stale, still-draining target from the
PREVIOUS deployment. That's wrong two ways: it can time out forever
waiting on a draining target that will never recover, AND — far worse — it
can report "healthy" using only the OLD target's state when the NEW task
hasn't registered a target yet at all, meaning cutover proceeds without
the new deployment's own task ever having been checked. That's exactly how
a real drill this session got a deliberately-broken app cut into 100%
production traffic undetected. The fix identifies the specific task(s)
belonging to the service's current (PRIMARY) ECS deployment and requires
ONLY those — matched by private IP — to report healthy, ignoring any other
target present. `test_does_not_report_healthy_from_a_stale_old_target_alone`
below is the regression test for the exact failure mode found live.

Fake boto3 clients only (ecs + elbv2), matching this repo's established
pattern for shared/aws_ecs_actuation.py's other functions — no real AWS
calls.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from shared import aws_ecs_actuation

CLUSTER = "smartcd-platform"
SERVICE = "checkout-canary"
NEW_TASK_DEF_ARN = "arn:aws:ecs:us-east-1:236087863083:task-definition/checkout-canary:5"
OLD_TASK_DEF_ARN = "arn:aws:ecs:us-east-1:236087863083:task-definition/checkout-canary:4"


class _FakeTargetGroupNotFound(Exception):
    pass


def _task(task_def_arn: str, private_ip: str) -> dict:
    return {
        "taskDefinitionArn": task_def_arn,
        "attachments": [{"details": [{"name": "privateIPv4Address", "value": private_ip}]}],
    }


class _FakeEcs:
    """`task_sequence`: each call to `describe_services` (the first AWS call
    `_get_current_deployment_task_private_ips` makes per poll) pops the next
    (task_def_arn_of_primary, tasks) pair and caches it for the SAME poll's
    subsequent `list_tasks`/`describe_tasks` calls — lets a test simulate the
    new task not having registered yet, then appearing, without the two
    calls disagreeing on which poll iteration they're in."""

    def __init__(self, task_sequence):
        self._task_sequence = list(task_sequence)
        self._current = self._task_sequence[0]

    def describe_services(self, cluster, services):
        self._current = self._task_sequence.pop(0) if len(self._task_sequence) > 1 else self._task_sequence[0]
        primary_task_def, _ = self._current
        return {"services": [{"deployments": [{"status": "PRIMARY", "taskDefinition": primary_task_def}]}]}

    def list_tasks(self, cluster, serviceName, desiredStatus):
        _, tasks = self._current
        return {"taskArns": [f"arn:aws:ecs:task/{i}" for i in range(len(tasks))]}

    def describe_tasks(self, cluster, tasks):
        _, task_list = self._current
        return {"tasks": task_list}


class _FakeElbv2:
    """`health_sequence`: each poll iteration pops the next list of (target_id, state) pairs."""

    def __init__(self, health_sequence):
        self._health_sequence = list(health_sequence)
        self.exceptions = type("Exceptions", (), {"TargetGroupNotFoundException": _FakeTargetGroupNotFound})()

    def describe_target_groups(self, Names):
        return {"TargetGroups": [{"TargetGroupArn": f"arn:aws:elasticloadbalancing:tg/{Names[0]}"}]}

    def describe_target_health(self, TargetGroupArn):
        states = self._health_sequence.pop(0) if len(self._health_sequence) > 1 else self._health_sequence[0]
        return {
            "TargetHealthDescriptions": [
                {"Target": {"Id": target_id}, "TargetHealth": {"State": state, "Reason": None}}
                for target_id, state in states
            ]
        }


def _install_fakes(monkeypatch, ecs, elbv2):
    monkeypatch.setattr(
        aws_ecs_actuation.boto3, "client",
        lambda service, region_name: ecs if service == "ecs" else elbv2,
    )
    monkeypatch.setattr(aws_ecs_actuation.time, "sleep", lambda s: None)


def test_returns_healthy_once_the_new_deployments_own_task_is_healthy(monkeypatch):
    ecs = _FakeEcs(task_sequence=[(NEW_TASK_DEF_ARN, [_task(NEW_TASK_DEF_ARN, "10.0.0.5")])])
    elbv2 = _FakeElbv2(health_sequence=[[("10.0.0.5", "healthy")]])
    _install_fakes(monkeypatch, ecs, elbv2)

    result = aws_ecs_actuation.wait_for_target_group_healthy(
        "us-east-1", "checkout-canary", cluster=CLUSTER, service_name=SERVICE
    )
    assert result["healthy"] is True
    assert result["targets"][0]["target"] == "10.0.0.5"


def test_waits_out_a_stale_draining_target_from_the_previous_deployment(monkeypatch):
    """
    Old target (previous deployment) is still registered and draining
    alongside the new, healthy one. Must NOT report healthy while the old
    target is still present at all — a live traffic check run immediately
    after this returns must be guaranteed to reach the new task, never the
    old one, so this waits until the old target is fully gone (not merely
    "draining"), then succeeds.
    """
    ecs = _FakeEcs(task_sequence=[(NEW_TASK_DEF_ARN, [_task(NEW_TASK_DEF_ARN, "10.0.0.5")])])
    elbv2 = _FakeElbv2(
        health_sequence=[
            [("10.0.0.5", "healthy"), ("10.0.0.4", "draining")],
            [("10.0.0.5", "healthy")],  # old target has now fully deregistered
        ]
    )
    _install_fakes(monkeypatch, ecs, elbv2)

    result = aws_ecs_actuation.wait_for_target_group_healthy(
        "us-east-1", "checkout-canary", cluster=CLUSTER, service_name=SERVICE, poll_interval_seconds=0.01
    )
    assert result["healthy"] is True
    assert [t["target"] for t in result["targets"]] == ["10.0.0.5"]


def test_does_not_report_healthy_from_a_stale_old_target_alone(monkeypatch):
    """
    The exact bug found live: the OLD target is healthy and is the ONLY
    thing currently registered in the target group — the new deployment's
    task hasn't registered a target yet. Must NOT report healthy (that
    would cut real traffic over having verified nothing about the new
    task) — must keep polling until the new task itself appears, is
    healthy, AND the old target is gone, or time out.
    """
    ecs = _FakeEcs(
        task_sequence=[
            (NEW_TASK_DEF_ARN, []),  # new task not registered as a target yet
            (NEW_TASK_DEF_ARN, [_task(NEW_TASK_DEF_ARN, "10.0.0.5")]),
        ]
    )
    elbv2 = _FakeElbv2(
        health_sequence=[
            [("10.0.0.4", "healthy")],  # only the OLD target is visible so far
            [("10.0.0.5", "healthy"), ("10.0.0.4", "draining")],  # new registers, old still present
            [("10.0.0.5", "healthy")],  # old target has now fully deregistered
        ]
    )
    _install_fakes(monkeypatch, ecs, elbv2)

    result = aws_ecs_actuation.wait_for_target_group_healthy(
        "us-east-1", "checkout-canary", cluster=CLUSTER, service_name=SERVICE, poll_interval_seconds=0.01
    )
    assert result["healthy"] is True
    assert [t["target"] for t in result["targets"]] == ["10.0.0.5"]


def test_polls_until_the_new_task_becomes_healthy(monkeypatch):
    ecs = _FakeEcs(task_sequence=[(NEW_TASK_DEF_ARN, [_task(NEW_TASK_DEF_ARN, "10.0.0.5")])])
    elbv2 = _FakeElbv2(
        health_sequence=[
            [("10.0.0.5", "unhealthy")],
            [("10.0.0.5", "unhealthy")],
            [("10.0.0.5", "healthy")],
        ]
    )
    sleep_calls = []
    monkeypatch.setattr(
        aws_ecs_actuation.boto3, "client",
        lambda service, region_name: ecs if service == "ecs" else elbv2,
    )
    monkeypatch.setattr(aws_ecs_actuation.time, "sleep", lambda s: sleep_calls.append(s))

    result = aws_ecs_actuation.wait_for_target_group_healthy(
        "us-east-1", "checkout-canary", cluster=CLUSTER, service_name=SERVICE, poll_interval_seconds=0.01
    )
    assert result["healthy"] is True
    assert len(sleep_calls) == 2


def test_raises_on_timeout_without_the_new_task_ever_confirmed_healthy(monkeypatch):
    ecs = _FakeEcs(task_sequence=[(NEW_TASK_DEF_ARN, [])])
    elbv2 = _FakeElbv2(health_sequence=[[("10.0.0.4", "healthy")]])
    _install_fakes(monkeypatch, ecs, elbv2)

    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0 if calls["n"] == 1 else 999

    monkeypatch.setattr(aws_ecs_actuation.time, "monotonic", fake_monotonic)

    with pytest.raises(RuntimeError, match="did not confirm the new deployment's own task"):
        aws_ecs_actuation.wait_for_target_group_healthy(
            "us-east-1", "checkout-canary", cluster=CLUSTER, service_name=SERVICE, timeout_seconds=1
        )


def test_raises_a_clear_error_when_target_group_does_not_exist(monkeypatch):
    ecs = _FakeEcs(task_sequence=[(NEW_TASK_DEF_ARN, [_task(NEW_TASK_DEF_ARN, "10.0.0.5")])])
    elbv2 = _FakeElbv2(health_sequence=[[("10.0.0.5", "healthy")]])

    def raise_not_found(Names):
        raise elbv2.exceptions.TargetGroupNotFoundException()

    elbv2.describe_target_groups = raise_not_found
    _install_fakes(monkeypatch, ecs, elbv2)

    with pytest.raises(RuntimeError, match="not found"):
        aws_ecs_actuation.wait_for_target_group_healthy(
            "us-east-1", "checkout-canary", cluster=CLUSTER, service_name=SERVICE
        )
