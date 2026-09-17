"""
shared/fargate_pricing.py

Centralized AWS Fargate on-demand pricing constants and unit converters.
Reference: published AWS Fargate on-demand Linux/x86 pricing (us-east-1).

Shared across services:
- pipeline-worker: for blue-green and ECS deploy cost tracking
- policy-controller: for canary rollout cost tracking (cost_tracker_ecs.py)
- api-gateway: for pre-onboarding cost estimation (repo_report.py)
"""
import os

FARGATE_CPU_COST_PER_VCPU_HOUR = float(os.environ.get("FARGATE_CPU_COST_PER_VCPU_HOUR", "0.040478"))
FARGATE_MEM_COST_PER_GB_HOUR = float(os.environ.get("FARGATE_MEM_COST_PER_GB_HOUR", "0.004446"))


def parse_fargate_cpu_vcpu(cpu_str: str | None) -> float:
    """Fargate task-level `cpu` is CPU units as a plain numeric string — 1024 units = 1 vCPU."""
    if not cpu_str:
        return 0.0
    return float(cpu_str) / 1024.0


def parse_fargate_memory_gib(memory_str: str | None) -> float:
    """Fargate task-level `memory` is MiB as a plain numeric string — 1024 MiB = 1 GiB."""
    if not memory_str:
        return 0.0
    return float(memory_str) / 1024.0


# Backward compatibility aliases
_parse_fargate_cpu_vcpu = parse_fargate_cpu_vcpu
_parse_fargate_memory_gib = parse_fargate_memory_gib
