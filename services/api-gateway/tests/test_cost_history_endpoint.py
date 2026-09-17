"""
services/api-gateway/tests/test_cost_history_endpoint.py

Covers get_project_cost_history (projects_router.py) — the new Cost tab's
one backend dependency. Real cost_analysis rows already recorded by
cost_tracker.py/cost_tracker_ecs.py, project-scoped and tenant-scoped.
`rightsizing_rec` is asserted to pass through exactly as stored — the
frontend, not this endpoint, is responsible for rendering a null one
honestly rather than a fabricated number (see the endpoint's own docstring
for why that's a deliberate, not accidental, choice).
"""
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

from src.routers import projects_router


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt, params=None):
        return FakeResult(self._rows)


class FakeRequest:
    def __init__(self, tenant_id="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant_id})()


def _row(cost_id, run_id, baseline, canary, delta, rec=None):
    return {
        "cost_id": cost_id,
        "pipeline_run_id": run_id,
        "baseline_cost": Decimal(str(baseline)),
        "canary_cost": Decimal(str(canary)),
        "delta_percent": Decimal(str(delta)),
        "rightsizing_rec": rec,
        "computed_at": datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc),
        "trigger_type": "GITHUB_PUSH",
        "commit_sha": "abc123",
        "commit_message": "fix: bug",
    }


def test_returns_rows_with_json_serializable_fields(monkeypatch):
    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    rows = [_row("cost-1", "run-1", 1.5, 1.7, 13.3)]
    result = asyncio.run(
        projects_router.get_project_cost_history("proj-1", FakeRequest(), db=FakeDB(rows))
    )

    entry = result["cost_history"][0]
    assert entry["cost_id"] == "cost-1"
    assert entry["pipeline_run_id"] == "run-1"
    assert entry["baseline_cost"] == 1.5
    assert entry["canary_cost"] == 1.7
    assert entry["delta_percent"] == 13.3
    assert entry["computed_at"] == "2026-09-16T12:00:00+00:00"
    assert entry["commit_sha"] == "abc123"
    assert isinstance(entry["baseline_cost"], float)


def test_rightsizing_rec_passes_through_null_honestly_not_fabricated(monkeypatch):
    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    rows = [_row("cost-1", "run-1", 1.0, 1.0, 0.0, rec=None)]
    result = asyncio.run(
        projects_router.get_project_cost_history("proj-1", FakeRequest(), db=FakeDB(rows))
    )

    assert result["cost_history"][0]["rightsizing_rec"] is None


def test_rightsizing_rec_passes_through_when_present(monkeypatch):
    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    rec = {"is_overprovisioned": True, "efficiency_cpu": 0.12, "recommended_cpu_vcpu": 0.1}
    rows = [_row("cost-1", "run-1", 2.0, 0.5, -75.0, rec=rec)]
    result = asyncio.run(
        projects_router.get_project_cost_history("proj-1", FakeRequest(), db=FakeDB(rows))
    )

    assert result["cost_history"][0]["rightsizing_rec"] == rec


def test_totals_are_summed_across_all_returned_rows(monkeypatch):
    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    rows = [_row("cost-1", "run-1", 1.0, 1.5, 50.0), _row("cost-2", "run-2", 2.0, 2.0, 0.0)]
    result = asyncio.run(
        projects_router.get_project_cost_history("proj-1", FakeRequest(), db=FakeDB(rows))
    )

    assert result["total_baseline_cost"] == 3.0
    assert result["total_canary_cost"] == 3.5


def test_empty_history_returns_empty_list_and_zero_totals(monkeypatch):
    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    result = asyncio.run(
        projects_router.get_project_cost_history("proj-1", FakeRequest(), db=FakeDB([]))
    )

    assert result == {"cost_history": [], "total_baseline_cost": 0.0, "total_canary_cost": 0.0}
