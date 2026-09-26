import os
import sys

# Repo root, so `import shared.<module>` resolves the same way it does
# inside every service's container (shared/ is mounted read-only into
# each). api-gateway had no tests/ directory at all before this — first
# real test coverage for this service.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _stub_infra_policy_engine(monkeypatch):
    """
    Every infra-draft router test would otherwise need a live OPA server (the router now independently
    checks each proposal - src/infra_policy.py), so the router-level call is stubbed to a clean bill of
    health by default. Tests of the policy behaviour itself either call src/infra_policy.py directly or
    override this stub with their own.
    """
    from src.routers import projects_router

    async def clean(proposal, intent_spec, existing_resources):
        return {"allowed": True, "deny": [], "warn": [], "checks": []}

    monkeypatch.setattr(projects_router, "evaluate_infra_policy", clean)


@_pytest.fixture(autouse=True)
def _stub_infra_cost_estimator(monkeypatch):
    """Router tests would otherwise call a live pipeline-worker to price templates; identity by default.
    Tests of the cost behaviour call src/infra_cost.py directly or override this stub."""
    from src.routers import projects_router

    async def unchanged(proposal, region):
        return proposal

    monkeypatch.setattr(projects_router, "apply_independent_cost", unchanged)
