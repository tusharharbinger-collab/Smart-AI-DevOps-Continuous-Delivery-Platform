"""
services/verification-engine/src/scoring/cost_analyzer.py

Cost-aware delivery & right-sizing — spec §10. Unit costs are configured via
env vars (production-readiness requirement), never hardcoded.

compute_cost_delta       — §10.1/§6a: canary vs. baseline compute cost delta.
compute_rightsizing_recommendation — §10.2/§6c: flags over-provisioned
    workloads. This ALWAYS returns a recommendation, NEVER auto-applies one —
    enforced independently by OPA Rule 8 (policies/delivery_guardrails.rego),
    which additionally requires a platform-admin approval signature (§6d).
"""
import os

CPU_COST_PER_VCPU_HOUR = float(os.environ.get("CPU_COST_PER_VCPU_HOUR", "0.0316"))
MEM_COST_PER_GIB_HOUR = float(os.environ.get("MEM_COST_PER_GIB_HOUR", "0.0042"))


def compute_cost_delta(
    canary_replicas: int,
    canary_cpu_vcpu: float,
    canary_mem_gib: float,
    baseline_replicas: int,
    baseline_cpu_vcpu: float,
    baseline_mem_gib: float,
    duration_hours: float = 1.0,
    max_permitted_delta_percent: float = 15.0,
) -> dict:
    """Computes the compute-cost delta between the canary and baseline cohorts."""
    canary_cost = canary_replicas * (
        CPU_COST_PER_VCPU_HOUR * canary_cpu_vcpu + MEM_COST_PER_GIB_HOUR * canary_mem_gib
    ) * duration_hours
    baseline_cost = baseline_replicas * (
        CPU_COST_PER_VCPU_HOUR * baseline_cpu_vcpu + MEM_COST_PER_GIB_HOUR * baseline_mem_gib
    ) * duration_hours

    delta = canary_cost - baseline_cost
    delta_percent = (delta / baseline_cost * 100) if baseline_cost > 0 else 0.0

    return {
        "canary_cost_usd": round(canary_cost, 6),
        "baseline_cost_usd": round(baseline_cost, 6),
        "delta_usd": round(delta, 6),
        "delta_percent": round(delta_percent, 2),
        "exceeds_policy_limit": delta_percent > max_permitted_delta_percent,
    }


def compute_rightsizing_recommendation(
    observed_cpu_p95: float,
    observed_mem_p95: float,
    requested_cpu: float,
    requested_mem: float,
    headroom: float = 0.25,
    min_cpu: float = 0.05,
    min_mem: float = 0.064,
    overprovisioned_threshold: float = 0.35,
) -> dict:
    """
    eta = p95_usage / requested. eta < threshold (default 0.35) flags
    over-provisioning. This is ALWAYS a recommendation; auto-apply is gated
    behind OPA Rule 8 requiring a platform-admin signature.
    """
    eta_cpu = observed_cpu_p95 / requested_cpu if requested_cpu > 0 else 1.0
    eta_mem = observed_mem_p95 / requested_mem if requested_mem > 0 else 1.0

    return {
        "efficiency_cpu": round(eta_cpu, 3),
        "efficiency_mem": round(eta_mem, 3),
        "is_overprovisioned": eta_cpu < overprovisioned_threshold or eta_mem < overprovisioned_threshold,
        "recommended_cpu_vcpu": round(max(min_cpu, observed_cpu_p95 * (1 + headroom)), 3),
        "recommended_mem_gib": round(max(min_mem, observed_mem_p95 * (1 + headroom)), 3),
        "action": "SUBMIT_GITOPS_PR",
        "requires_approval_role": "platform-admin",
    }
