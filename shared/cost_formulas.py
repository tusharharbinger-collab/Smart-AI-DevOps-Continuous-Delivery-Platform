"""
shared/cost_formulas.py

Guaranteed Live Web App CI/CD — real gap this closes: blue-green rollouts
(worker.py's health-gated canary_loop branch, pipeline-worker) never went
through policy-controller's verdict-driven controller.py, which is the ONLY
place `services/policy-controller/src/cost_tracker.py`/`cost_tracker_ecs.py`
were ever called from — so `cost_analysis` has had 0 rows for any blue-green
rollout, on any project, since blue-green was built. This is the exact same
category of gap `cost_tracker_ecs.py`'s own module docstring already
describes being found and fixed once for the Kubernetes/ECS split; it just
hadn't been found yet for the canary-vs-blue-green split.

This is the ONE pure, stateless formula both `cost_tracker.py` (Kubernetes,
policy-controller) and pipeline-worker's own blue-green cost computation
need — moved here (not duplicated a third time) for the same reason
`shared/aws_ecs_actuation.py` centralizes traffic-weight-shifting logic:
pipeline-worker and policy-controller are separate Docker build contexts
with no shared package boundary except `shared/`, and this arithmetic
computing real dollar costs that gate a real guardrail (RULE 7) is exactly
the category of logic this codebase already treats as needing one source of
truth, never two copies that could drift. `cost_tracker.py`'s own
`_compute_cost_delta` now delegates here rather than duplicating the body.
"""


def compute_cost_delta(
    canary_replicas: int,
    canary_cpu_vcpu: float,
    canary_mem_gib: float,
    baseline_replicas: int,
    baseline_cpu_vcpu: float,
    baseline_mem_gib: float,
    max_permitted_delta_percent: float,
    duration_hours: float,
    cpu_rate: float,
    mem_rate: float,
) -> dict:
    canary_cost = canary_replicas * (
        cpu_rate * canary_cpu_vcpu + mem_rate * canary_mem_gib
    ) * duration_hours
    baseline_cost = baseline_replicas * (
        cpu_rate * baseline_cpu_vcpu + mem_rate * baseline_mem_gib
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
