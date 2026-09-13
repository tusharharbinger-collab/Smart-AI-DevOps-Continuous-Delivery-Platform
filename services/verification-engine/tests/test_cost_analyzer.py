"""
services/verification-engine/tests/test_cost_analyzer.py

Spec §10 / rubric items 6a-6c.
"""
from src.scoring.cost_analyzer import compute_cost_delta, compute_rightsizing_recommendation


def test_cost_delta_flags_over_budget_canary():
    result = compute_cost_delta(
        canary_replicas=4, canary_cpu_vcpu=1.0, canary_mem_gib=2.0,
        baseline_replicas=2, baseline_cpu_vcpu=1.0, baseline_mem_gib=2.0,
    )
    assert result["delta_percent"] > 15.0
    assert result["exceeds_policy_limit"] is True


def test_cost_delta_within_budget_not_flagged():
    result = compute_cost_delta(
        canary_replicas=2, canary_cpu_vcpu=1.0, canary_mem_gib=2.0,
        baseline_replicas=2, baseline_cpu_vcpu=1.0, baseline_mem_gib=2.0,
    )
    assert result["delta_percent"] == 0.0
    assert result["exceeds_policy_limit"] is False


def test_rightsizing_flags_overprovisioned_workload():
    result = compute_rightsizing_recommendation(
        observed_cpu_p95=0.05, observed_mem_p95=0.1,
        requested_cpu=1.0, requested_mem=2.0,
    )
    assert result["is_overprovisioned"] is True
    assert result["requires_approval_role"] == "platform-admin"
    assert result["action"] == "SUBMIT_GITOPS_PR"


def test_rightsizing_does_not_flag_well_sized_workload():
    result = compute_rightsizing_recommendation(
        observed_cpu_p95=0.9, observed_mem_p95=1.8,
        requested_cpu=1.0, requested_mem=2.0,
    )
    assert result["is_overprovisioned"] is False


def test_rightsizing_never_auto_applies():
    """The recommendation object itself carries no path to auto-apply —
    action is always the human-in-the-loop GitOps PR, never a direct patch."""
    result = compute_rightsizing_recommendation(
        observed_cpu_p95=0.01, observed_mem_p95=0.01,
        requested_cpu=1.0, requested_mem=2.0,
    )
    assert result["action"] == "SUBMIT_GITOPS_PR"
    assert "auto_apply" not in result
