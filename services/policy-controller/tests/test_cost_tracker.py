"""
services/policy-controller/tests/test_cost_tracker.py

Real gap found live: RULE 7 in policies/delivery_guardrails.rego
(`cost_delta_exceeds_limit`) compares `input.cost_analysis.delta_percent`
against `maxPermittedCostDeltaPercent`, but controller.py always sent a
hardcoded `{"delta_percent": 0.0}` — the guardrail could never fire no
matter how expensive a canary actually was, and cost_analysis has had 0
rows since day one. These tests cover cost_tracker.py's pure parsing/
formula functions (no live cluster needed) and its fail-soft behavior when
Kubernetes is unreachable or a Deployment doesn't exist.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from unittest.mock import MagicMock

import pytest

from src.cost_tracker import (
    _parse_cpu_vcpu,
    _parse_memory_gib,
    _compute_cost_delta,
    _read_deployment_footprint,
    compute_and_record_cost,
)


def test_parse_cpu_millicores():
    assert _parse_cpu_vcpu("250m") == 0.25
    assert _parse_cpu_vcpu("50m") == 0.05


def test_parse_cpu_bare_cores():
    assert _parse_cpu_vcpu("1") == 1.0
    assert _parse_cpu_vcpu("2.5") == 2.5


def test_parse_cpu_none_or_missing_is_zero():
    assert _parse_cpu_vcpu(None) == 0.0
    assert _parse_cpu_vcpu("") == 0.0


def test_parse_memory_binary_units():
    assert _parse_memory_gib("1Gi") == 1.0
    assert round(_parse_memory_gib("256Mi"), 4) == round(256 / 1024, 4)
    assert round(_parse_memory_gib("64Mi"), 4) == round(64 / 1024, 4)


def test_parse_memory_bare_bytes():
    one_gib_in_bytes = str(1024 ** 3)
    assert round(_parse_memory_gib(one_gib_in_bytes), 4) == 1.0


def test_compute_cost_delta_flags_over_budget_canary():
    result = _compute_cost_delta(
        canary_replicas=4, canary_cpu_vcpu=1.0, canary_mem_gib=2.0,
        baseline_replicas=2, baseline_cpu_vcpu=1.0, baseline_mem_gib=2.0,
        max_permitted_delta_percent=15.0,
    )
    assert result["delta_percent"] > 15.0
    assert result["exceeds_policy_limit"] is True


def test_compute_cost_delta_within_budget_not_flagged():
    result = _compute_cost_delta(
        canary_replicas=2, canary_cpu_vcpu=1.0, canary_mem_gib=2.0,
        baseline_replicas=2, baseline_cpu_vcpu=1.0, baseline_mem_gib=2.0,
        max_permitted_delta_percent=15.0,
    )
    assert result["delta_percent"] == 0.0
    assert result["exceeds_policy_limit"] is False


def test_compute_cost_delta_defaults_to_module_kubernetes_rates():
    import src.cost_tracker as cost_tracker_module

    result = _compute_cost_delta(
        canary_replicas=1, canary_cpu_vcpu=1.0, canary_mem_gib=1.0,
        baseline_replicas=1, baseline_cpu_vcpu=1.0, baseline_mem_gib=1.0,
        max_permitted_delta_percent=15.0,
    )
    expected = cost_tracker_module.CPU_COST_PER_VCPU_HOUR + cost_tracker_module.MEM_COST_PER_GIB_HOUR
    assert result["canary_cost_usd"] == round(expected, 6)


def test_compute_cost_delta_accepts_overridden_rates_for_a_different_target():
    # cost_tracker_ecs.py reuses this exact formula against real AWS Fargate
    # rates instead of the Kubernetes-node-estimate module constants — this
    # proves overriding the rate actually changes the computed cost and
    # doesn't silently fall back to the Kubernetes defaults.
    result_default = _compute_cost_delta(
        canary_replicas=1, canary_cpu_vcpu=1.0, canary_mem_gib=1.0,
        baseline_replicas=1, baseline_cpu_vcpu=1.0, baseline_mem_gib=1.0,
        max_permitted_delta_percent=15.0,
    )
    result_overridden = _compute_cost_delta(
        canary_replicas=1, canary_cpu_vcpu=1.0, canary_mem_gib=1.0,
        baseline_replicas=1, baseline_cpu_vcpu=1.0, baseline_mem_gib=1.0,
        max_permitted_delta_percent=15.0,
        cpu_rate=0.040478, mem_rate=0.004446,
    )
    assert result_overridden["canary_cost_usd"] == round(0.040478 + 0.004446, 6)
    assert result_overridden["canary_cost_usd"] != result_default["canary_cost_usd"]


def test_read_deployment_footprint_returns_none_when_not_found():
    from kubernetes.client.rest import ApiException

    fake_apps_v1 = MagicMock()
    fake_apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)

    result = _read_deployment_footprint(fake_apps_v1, "missing-deployment", "production")

    assert result is None


def test_read_deployment_footprint_parses_real_looking_deployment():
    fake_deployment = MagicMock()
    fake_deployment.spec.replicas = 3
    container = MagicMock()
    container.resources.requests = {"cpu": "100m", "memory": "128Mi"}
    fake_deployment.spec.template.spec.containers = [container]

    fake_apps_v1 = MagicMock()
    fake_apps_v1.read_namespaced_deployment.return_value = fake_deployment

    result = _read_deployment_footprint(fake_apps_v1, "svc-canary", "production")

    assert result["replicas"] == 3
    assert result["cpu_vcpu"] == 0.1
    assert round(result["mem_gib"], 4) == round(128 / 1024, 4)


def test_compute_and_record_cost_is_fail_soft_when_kube_unreachable(monkeypatch):
    import src.cost_tracker as cost_tracker_module

    def raise_always():
        raise RuntimeError("no kubeconfig available in this environment")

    monkeypatch.setattr(cost_tracker_module, "_load_kube", raise_always)

    result = asyncio.run(
        compute_and_record_cost(
            pipeline_run_id="run-cost-test",
            tenant_id="tenant-1",
            namespace="production",
            baseline_deployment_name="svc-baseline",
            canary_deployment_name="svc-canary",
            db=None,
        )
    )

    assert result is None, "an unreachable cluster must never raise or block the caller"


def test_compute_and_record_cost_writes_to_db_when_available(monkeypatch):
    import src.cost_tracker as cost_tracker_module

    monkeypatch.setattr(cost_tracker_module, "_load_kube", lambda: None)
    monkeypatch.setattr(cost_tracker_module.k8s_client, "AppsV1Api", MagicMock())

    def fake_footprint(apps_v1, name, namespace):
        if "canary" in name:
            return {"replicas": 4, "cpu_vcpu": 1.0, "mem_gib": 2.0}
        return {"replicas": 2, "cpu_vcpu": 1.0, "mem_gib": 2.0}

    monkeypatch.setattr(cost_tracker_module, "_read_deployment_footprint", fake_footprint)

    written = {}

    class FakeDB:
        async def record_cost_analysis(self, **kwargs):
            written.update(kwargs)

    result = asyncio.run(
        compute_and_record_cost(
            pipeline_run_id="run-cost-test",
            tenant_id="tenant-1",
            namespace="production",
            baseline_deployment_name="svc-baseline",
            canary_deployment_name="svc-canary",
            db=FakeDB(),
            max_permitted_delta_percent=15.0,
        )
    )

    assert result["exceeds_policy_limit"] is True
    assert written["pipeline_run_id"] == "run-cost-test"
    assert written["delta_percent"] == result["delta_percent"]
