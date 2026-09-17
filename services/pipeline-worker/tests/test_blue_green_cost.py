"""
services/pipeline-worker/tests/test_blue_green_cost.py

Guaranteed Live Web App CI/CD — real gap found live (2026-09-17):
cost_analysis has had 0 rows for any blue-green rollout on any project,
ever. RULE 7 in policies/delivery_guardrails.rego and the Reports & Cost UI
both read from a table only policy-controller's verdict-driven controller.py
ever wrote to, which worker.py's health-gated blue-green path never
reaches. Mirrors test_cost_tracker_ecs.py's fake-boto3-client pattern — no
real AWS calls.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import src.aws.ecs_deploy_task as ecs_deploy_task


class _FakeEcs:
    def __init__(self, services: dict):
        # services: {service_name: {"desiredCount": int, "cpu": "256", "memory": "512"}}
        self._services = services

    def describe_services(self, cluster, services):
        name = services[0]
        if name not in self._services:
            return {"services": []}
        return {"services": [{"status": "ACTIVE", "desiredCount": self._services[name]["desiredCount"]}]}

    def describe_task_definition(self, taskDefinition):
        raise NotImplementedError  # not used — describe_current_container_config is monkeypatched instead


def _fake_describe_current_container_config(ecs, cluster, service_name):
    cpu_mem = {"checkout-baseline": ("512", "1024"), "checkout-canary": ("512", "1024")}
    cpu, memory = cpu_mem.get(service_name, ("256", "512"))
    return {"cpu": cpu, "memory": memory}


def test_compute_blue_green_cost_reads_live_footprints_and_computes_delta(monkeypatch):
    fake_ecs = _FakeEcs({"checkout-baseline": {"desiredCount": 1}, "checkout-canary": {"desiredCount": 1}})
    monkeypatch.setattr(ecs_deploy_task.boto3, "client", lambda service, region_name: fake_ecs)
    monkeypatch.setattr(ecs_deploy_task, "describe_current_container_config", _fake_describe_current_container_config)

    result = ecs_deploy_task.compute_blue_green_cost("checkout", "us-east-1")

    assert result is not None
    # Identical footprints on both sides -> zero delta.
    assert result["delta_percent"] == 0.0
    assert result["baseline_cost_usd"] == result["canary_cost_usd"]


def test_compute_blue_green_cost_is_fail_soft_when_baseline_service_missing(monkeypatch):
    # Canary exists, baseline doesn't — e.g. onboarding never completed.
    fake_ecs = _FakeEcs({"checkout-canary": {"desiredCount": 1}})
    monkeypatch.setattr(ecs_deploy_task.boto3, "client", lambda service, region_name: fake_ecs)
    monkeypatch.setattr(ecs_deploy_task, "describe_current_container_config", _fake_describe_current_container_config)

    assert ecs_deploy_task.compute_blue_green_cost("checkout", "us-east-1") is None


def test_compute_blue_green_cost_is_fail_soft_when_aws_unreachable(monkeypatch):
    def raise_error(service, region_name):
        raise Exception("could not connect to the endpoint URL")

    monkeypatch.setattr(ecs_deploy_task.boto3, "client", raise_error)

    # Must never raise — a cost snapshot failing must never fail the
    # actual rollout it's attached to.
    assert ecs_deploy_task.compute_blue_green_cost("checkout", "us-east-1") is None


def test_compute_blue_green_cost_flags_an_expensive_canary(monkeypatch):
    fake_ecs = _FakeEcs({"checkout-baseline": {"desiredCount": 1}, "checkout-canary": {"desiredCount": 3}})
    monkeypatch.setattr(ecs_deploy_task.boto3, "client", lambda service, region_name: fake_ecs)
    monkeypatch.setattr(ecs_deploy_task, "describe_current_container_config", _fake_describe_current_container_config)

    result = ecs_deploy_task.compute_blue_green_cost("checkout", "us-east-1", max_permitted_delta_percent=15.0)

    assert result["delta_percent"] > 15.0
    assert result["exceeds_policy_limit"] is True
