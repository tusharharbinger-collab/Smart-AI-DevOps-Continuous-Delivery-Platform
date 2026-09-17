"""
services/policy-controller/tests/test_cost_tracker_ecs.py

Real gap found live: controller.py has skipped cost-gating entirely for
every `deployment_target == "aws_ecs"` project — RULE 7
(cost_delta_exceeds_limit) could never fire for an AWS project no matter
how expensive a canary actually was, and cost_analysis has had 0 rows for
any AWS-deployed project since that target existed (the identical bug
test_cost_tracker.py already covers for Kubernetes). These tests cover
cost_tracker_ecs.py's pure parsing/formula functions (no live AWS needed)
and its fail-soft behavior when ECS is unreachable or a service doesn't
exist yet. Mirrors test_cost_tracker.py's style exactly.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from unittest.mock import MagicMock

import pytest

from src.cost_tracker_ecs import (
    _parse_fargate_cpu_vcpu,
    _parse_fargate_memory_gib,
    _read_ecs_service_footprint,
    compute_and_record_cost_ecs,
)


def test_parse_fargate_cpu_units():
    # Fargate task-level cpu is CPU units, 1024 units = 1 vCPU.
    assert _parse_fargate_cpu_vcpu("1024") == 1.0
    assert _parse_fargate_cpu_vcpu("256") == 0.25
    assert _parse_fargate_cpu_vcpu("512") == 0.5


def test_parse_fargate_cpu_none_or_missing_is_zero():
    assert _parse_fargate_cpu_vcpu(None) == 0.0
    assert _parse_fargate_cpu_vcpu("") == 0.0


def test_parse_fargate_memory_mib_to_gib():
    # Fargate task-level memory is MiB.
    assert _parse_fargate_memory_gib("1024") == 1.0
    assert round(_parse_fargate_memory_gib("512"), 4) == round(512 / 1024, 4)


def test_parse_fargate_memory_none_or_missing_is_zero():
    assert _parse_fargate_memory_gib(None) == 0.0
    assert _parse_fargate_memory_gib("") == 0.0


def test_read_ecs_service_footprint_returns_none_when_service_not_active():
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {"services": []}

    result = _read_ecs_service_footprint(fake_ecs, "smartcd-platform", "missing-canary")

    assert result is None


def test_read_ecs_service_footprint_returns_none_when_describe_services_raises():
    fake_ecs = MagicMock()
    fake_ecs.describe_services.side_effect = RuntimeError("AWS unreachable")

    result = _read_ecs_service_footprint(fake_ecs, "smartcd-platform", "svc-canary")

    assert result is None


def test_read_ecs_service_footprint_parses_real_looking_service(monkeypatch):
    import src.cost_tracker_ecs as module

    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {
        "services": [{"status": "ACTIVE", "desiredCount": 2}]
    }
    monkeypatch.setattr(
        module, "describe_current_container_config",
        lambda ecs, cluster, service_name: {"cpu": "512", "memory": "1024"},
    )

    result = _read_ecs_service_footprint(fake_ecs, "smartcd-platform", "svc-canary")

    assert result == {"desired_count": 2, "cpu_vcpu": 0.5, "mem_gib": 1.0}


def test_compute_and_record_cost_ecs_is_fail_soft_when_aws_unreachable(monkeypatch):
    import src.cost_tracker_ecs as module

    def raise_always(service, region_name):
        raise RuntimeError("no AWS credentials available in this environment")

    monkeypatch.setattr(module.boto3, "client", raise_always)

    result = asyncio.run(
        compute_and_record_cost_ecs(
            pipeline_run_id="run-ecs-cost-test",
            tenant_id="tenant-1",
            baseline_service_name="checkout-baseline",
            canary_service_name="checkout-canary",
            region="us-east-1",
            db=None,
        )
    )

    assert result is None, "unreachable AWS must never raise or block the caller"


def test_compute_and_record_cost_ecs_is_fail_soft_when_service_missing(monkeypatch):
    import src.cost_tracker_ecs as module

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: MagicMock())
    monkeypatch.setattr(module, "_read_ecs_service_footprint", lambda ecs, cluster, name: None)

    result = asyncio.run(
        compute_and_record_cost_ecs(
            pipeline_run_id="run-ecs-cost-test",
            tenant_id="tenant-1",
            baseline_service_name="checkout-baseline",
            canary_service_name="checkout-canary",
            region="us-east-1",
            db=None,
        )
    )

    assert result is None


def test_compute_and_record_cost_ecs_writes_to_db_and_uses_fargate_rates(monkeypatch):
    import src.cost_tracker_ecs as module

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: MagicMock())

    def fake_footprint(ecs, cluster, service_name):
        if "canary" in service_name:
            return {"desired_count": 4, "cpu_vcpu": 1.0, "mem_gib": 2.0}
        return {"desired_count": 2, "cpu_vcpu": 1.0, "mem_gib": 2.0}

    monkeypatch.setattr(module, "_read_ecs_service_footprint", fake_footprint)

    written = {}

    class FakeDB:
        async def record_cost_analysis(self, **kwargs):
            written.update(kwargs)

    result = asyncio.run(
        compute_and_record_cost_ecs(
            pipeline_run_id="run-ecs-cost-test",
            tenant_id="tenant-1",
            baseline_service_name="checkout-baseline",
            canary_service_name="checkout-canary",
            region="us-east-1",
            db=FakeDB(),
            max_permitted_delta_percent=15.0,
        )
    )

    expected_canary_cost = 4 * (module.FARGATE_CPU_COST_PER_VCPU_HOUR * 1.0 + module.FARGATE_MEM_COST_PER_GB_HOUR * 2.0)
    expected_baseline_cost = 2 * (module.FARGATE_CPU_COST_PER_VCPU_HOUR * 1.0 + module.FARGATE_MEM_COST_PER_GB_HOUR * 2.0)

    assert result["canary_cost_usd"] == round(expected_canary_cost, 6)
    assert result["baseline_cost_usd"] == round(expected_baseline_cost, 6)
    assert result["exceeds_policy_limit"] is True, "doubling desired count at equal footprint must exceed a 15% ceiling"
    assert written["pipeline_run_id"] == "run-ecs-cost-test"
    assert written["delta_percent"] == result["delta_percent"]


def test_compute_and_record_cost_ecs_skips_db_write_without_tenant_id(monkeypatch):
    import src.cost_tracker_ecs as module

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: MagicMock())
    monkeypatch.setattr(
        module, "_read_ecs_service_footprint",
        lambda ecs, cluster, name: {"desired_count": 1, "cpu_vcpu": 0.5, "mem_gib": 1.0},
    )

    class ExplodingDB:
        async def record_cost_analysis(self, **kwargs):
            raise AssertionError("must not be called when tenant_id is None")

    result = asyncio.run(
        compute_and_record_cost_ecs(
            pipeline_run_id="run-ecs-cost-test",
            tenant_id=None,
            baseline_service_name="checkout-baseline",
            canary_service_name="checkout-canary",
            region="us-east-1",
            db=ExplodingDB(),
        )
    )

    assert result is not None
