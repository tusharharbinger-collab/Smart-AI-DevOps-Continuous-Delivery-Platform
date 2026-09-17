"""
services/pipeline-worker/tests/test_ecs_deploy_task.py

Module 8 continuation — real gap this covers: `onboard_ecs_service` only
ever ran once, at project creation. A genuine `git push` builds and pushes
a NEW image tag to ECR, but nothing deployed it to the already-running ECS
canary service. Covers `_deploy_ecs_cohort` (used by both
`deploy_ecs_canary_task` and `deploy_ecs_baseline_task`): it must read the
cohort's CURRENTLY running task definition and clone it with only the
image swapped (never require port/cpu/memory to be re-supplied), and
`set_first_deployment_ecs_weights`'s real OPA freeze-window gate.

Fake boto3 clients only — no real AWS calls, matching this repo's
established pattern (test_ecs_onboarding.py, test_deprovisioning.py).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import src.aws.ecs_deploy_task as ecs_deploy_task


class _FakeEcsForDeploy:
    def __init__(self, current_image="123.dkr.ecr.us-east-1.amazonaws.com/svc:v1.0.0"):
        self.registered = []
        self.updated = []
        self._task_def_arn = "arn:aws:ecs:us-east-1:123:task-definition/svc-canary:1"
        self._current_image = current_image

    def describe_services(self, cluster, services):
        return {"services": [{"status": "ACTIVE", "taskDefinition": self._task_def_arn}]}

    def describe_task_definition(self, taskDefinition):
        return {
            "taskDefinition": {
                "family": "svc-canary",
                "cpu": "256",
                "memory": "512",
                "executionRoleArn": "arn:aws:iam::123:role/smartcd-ecsTaskExecutionRole",
                "containerDefinitions": [
                    {
                        "name": "svc",
                        "image": self._current_image,
                        "portMappings": [{"containerPort": 8080}],
                        "logConfiguration": {"options": {"awslogs-group": "/ecs/svc"}},
                    }
                ],
            }
        }

    def register_task_definition(self, **kwargs):
        self.registered.append(kwargs)
        return {"taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/svc-canary:2"}}

    def update_service(self, **kwargs):
        self.updated.append(kwargs)


class _FakeEc2ForDeploy:
    def describe_vpcs(self, Filters):
        return {"Vpcs": [{"VpcId": "vpc-1"}]}

    def describe_subnets(self, Filters):
        return {"Subnets": [{"SubnetId": "subnet-1"}, {"SubnetId": "subnet-2"}]}

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": [{"GroupId": "sg-task"}]}


class _FakeElbv2ForDeploy:
    def describe_target_groups(self, Names):
        return {"TargetGroups": [{"TargetGroupArn": f"arn-{Names[0]}"}]}


def _patch_clients(monkeypatch, ecs, ec2=None, elbv2=None):
    clients = {"ecs": ecs, "ec2": ec2 or _FakeEc2ForDeploy(), "elbv2": elbv2 or _FakeElbv2ForDeploy(), "iam": None, "logs": None}
    monkeypatch.setattr(ecs_deploy_task, "_clients", lambda region: clients)


def test_deploy_ecs_canary_task_clones_current_config_with_only_image_swapped(monkeypatch):
    ecs = _FakeEcsForDeploy()
    _patch_clients(monkeypatch, ecs)

    result = ecs_deploy_task.deploy_ecs_canary_task(
        "run-1", "svc", "123.dkr.ecr.us-east-1.amazonaws.com/svc", "v1.1.0", "us-east-1"
    )

    assert result["status"] == "updated"
    assert len(ecs.registered) == 1
    reg = ecs.registered[0]
    assert reg["family"] == "svc-canary"
    assert reg["containerDefinitions"][0]["image"] == "123.dkr.ecr.us-east-1.amazonaws.com/svc:v1.1.0"
    # port/cpu/memory/execution role/container name all carried over from
    # the EXISTING task definition, never re-supplied by the caller.
    assert reg["containerDefinitions"][0]["portMappings"][0]["containerPort"] == 8080
    assert reg["cpu"] == "256" and reg["memory"] == "512"
    assert reg["containerDefinitions"][0]["name"] == "svc"
    env_vars = {e["name"]: e["value"] for e in reg["containerDefinitions"][0]["environment"]}
    assert env_vars["DEPLOYMENT_COHORT"] == "canary"
    assert env_vars["APP_VERSION"] == "v1.1.0"
    assert env_vars["PATH_PREFIX"] == ""
    assert len(ecs.updated) == 1
    assert ecs.updated[0]["service"] == "svc-canary"
    assert ecs.updated[0]["taskDefinition"] == "arn:aws:ecs:us-east-1:123:task-definition/svc-canary:2"


def test_deploy_ecs_canary_task_preserves_path_prefix_from_current_task_def(monkeypatch):
    ecs = _FakeEcsForDeploy()
    orig_describe = ecs.describe_task_definition
    def _describe_with_prefix(taskDefinition):
        resp = orig_describe(taskDefinition)
        resp["taskDefinition"]["containerDefinitions"][0]["environment"] = [
            {"name": "PATH_PREFIX", "value": "/api/v1/checkout"},
            {"name": "DEPLOYMENT_COHORT", "value": "canary"},
            {"name": "APP_VERSION", "value": "v1.0.0"},
        ]
        return resp
    ecs.describe_task_definition = _describe_with_prefix
    _patch_clients(monkeypatch, ecs)

    result = ecs_deploy_task.deploy_ecs_canary_task(
        "run-2", "svc", "123.dkr.ecr.us-east-1.amazonaws.com/svc", "v1.2.0", "us-east-1"
    )

    assert result["status"] == "updated"
    reg = ecs.registered[0]
    env_vars = {e["name"]: e["value"] for e in reg["containerDefinitions"][0]["environment"]}
    assert env_vars["PATH_PREFIX"] == "/api/v1/checkout"
    assert env_vars["APP_VERSION"] == "v1.2.0"


def test_deploy_ecs_baseline_task_targets_the_baseline_service(monkeypatch):
    ecs = _FakeEcsForDeploy()
    _patch_clients(monkeypatch, ecs)

    ecs_deploy_task.deploy_ecs_baseline_task(
        "run-1", "svc", "123.dkr.ecr.us-east-1.amazonaws.com/svc", "v1.1.0", "us-east-1"
    )

    assert ecs.updated[0]["service"] == "svc-baseline"


def test_deploy_fails_clearly_when_project_was_never_onboarded(monkeypatch):
    ecs = _FakeEcsForDeploy()
    ec2_no_sg = _FakeEc2ForDeploy()
    ec2_no_sg.describe_security_groups = lambda Filters: {"SecurityGroups": []}
    _patch_clients(monkeypatch, ecs, ec2=ec2_no_sg)

    with pytest.raises(RuntimeError, match="never onboarded"):
        ecs_deploy_task.deploy_ecs_canary_task(
            "run-1", "svc", "123.dkr.ecr.us-east-1.amazonaws.com/svc", "v1.1.0", "us-east-1"
        )


def test_set_first_deployment_ecs_weights_calls_set_traffic_weights_100_0(monkeypatch):
    calls = []
    monkeypatch.setattr(ecs_deploy_task, "set_traffic_weights", lambda *a, **k: calls.append((a, k)) or {"status": "weights_updated"})
    monkeypatch.setattr(
        ecs_deploy_task, "evaluate_policy_sync", lambda opa_input: {"allow_action": True}
    )

    result = ecs_deploy_task.set_first_deployment_ecs_weights(
        "run-1", "svc", "/api/v1/svc", "us-east-1", pipeline_policy={"gates": {}, "guardrails": {}}
    )

    assert result["status"] == "weights_updated"
    args, kwargs = calls[0]
    assert args == ("us-east-1", "svc", "/api/v1/svc")
    assert kwargs == {"baseline_weight": 100, "canary_weight": 0}


def test_set_first_deployment_ecs_weights_blocked_by_freeze_window(monkeypatch):
    monkeypatch.setattr(
        ecs_deploy_task, "evaluate_policy_sync",
        lambda opa_input: {"allow_action": False, "rejection_reasons": ["freeze window active"]},
    )
    with pytest.raises(RuntimeError, match="blocked by policy"):
        ecs_deploy_task.set_first_deployment_ecs_weights(
            "run-1", "svc", "/api/v1/svc", "us-east-1", pipeline_policy={"gates": {}, "guardrails": {}}
        )
