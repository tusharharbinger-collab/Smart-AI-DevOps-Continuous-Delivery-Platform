"""Backlog #4 - POST /infra-drafts/{id}/failure-analysis: read-only, grounded in real events, degrades gracefully."""
import asyncio

import pytest
from fastapi import HTTPException

from src.routers import projects_router
from tests.test_infra_import_and_edit_endpoints import DRAFT_ID, FakeDB, FakeRequest, _Client, _Resp, _row

CONN = {"role_arn": "arn:aws:iam::123456789012:role/smartcd-platform-access", "external_id": "e" * 43}
RCA = {"likely_cause": "x", "evidence": ["e"], "suggested_fix": "f", "suggested_edit_instruction": None,
       "category": "unknown", "source": "model"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def conn_stub(monkeypatch):
    async def load(db, tenant_id, connection_id):
        return dict(CONN) if connection_id else None
    monkeypatch.setattr(projects_router, "load_provisioning_connection", load)


def db_with(**over):
    db = FakeDB()
    db.rows[DRAFT_ID] = _row(**over)
    return db


def test_wrong_status_is_409(monkeypatch):
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({}))
    with pytest.raises(HTTPException) as e:
        run(projects_router.analyze_infra_draft_failure(DRAFT_ID, FakeRequest(), db=db_with(status="INFRA_PROVISIONED")))
    assert e.value.status_code == 409


def test_unknown_draft_is_404(monkeypatch):
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({}))
    with pytest.raises(HTTPException) as e:
        run(projects_router.analyze_infra_draft_failure(DRAFT_ID, FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 404


def test_provisioning_failure_sends_real_events_and_connection(monkeypatch):
    client = _Client({"failure-events": _Resp(200, {"events": [{"resource": "Db"}]}), "infra-failure-rca": _Resp(200, RCA)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = db_with(status="INFRA_PROVISIONING_FAILED", stack_name="s1", provisioning_error="rolled back",
                 aws_connection_id="c1")
    out = run(projects_router.analyze_infra_draft_failure(DRAFT_ID, FakeRequest(), db=db))
    assert out["phase"] == "stack" and out["events_examined"] == 1 and out["source"] == "model"
    ev_call = next(c for c in client.calls if "failure-events" in c[1])[2]
    assert ev_call["connection"] == CONN and ev_call["stack_name"] == "s1"
    rca_call = next(c for c in client.calls if "infra-failure-rca" in c[1])[2]
    assert rca_call["status_reason"] == "rolled back" and "connection" not in rca_call


def test_change_set_failure_without_stack_still_analyzes(monkeypatch):
    client = _Client({"infra-failure-rca": _Resp(200, RCA)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = db_with(status="INFRA_CHANGE_SET_FAILED", stack_name=None, provisioning_error="bad template")
    out = run(projects_router.analyze_infra_draft_failure(DRAFT_ID, FakeRequest(), db=db))
    assert out["phase"] == "change_set" and out["events_examined"] == 0
    assert not any("failure-events" in c[1] for c in client.calls)


def test_events_unavailable_degrades(monkeypatch):
    client = _Client({"failure-events": _Resp(500, None, "x"), "infra-failure-rca": _Resp(200, RCA)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = db_with(status="INFRA_PROVISIONING_FAILED", stack_name="s1")
    assert run(projects_router.analyze_infra_draft_failure(DRAFT_ID, FakeRequest(), db=db))["events_examined"] == 0


def test_rca_service_down_is_502(monkeypatch):
    client = _Client({"infra-failure-rca": _Resp(503, None, "down")})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = db_with(status="INFRA_CHANGE_SET_FAILED")
    with pytest.raises(HTTPException) as e:
        run(projects_router.analyze_infra_draft_failure(DRAFT_ID, FakeRequest(), db=db))
    assert e.value.status_code == 502
