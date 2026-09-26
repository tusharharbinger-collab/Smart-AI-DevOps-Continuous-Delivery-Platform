"""
services/api-gateway/tests/test_infra_provisioning_execution_endpoint.py

Covers create_infra_change_set/execute_infra_change_set/
get_infra_provisioning_status (projects_router.py) — Phase 7's real
provisioning-execution endpoints, per AI_INFRA_PROVISIONING_EXECUTION_PLAN.md.
Same FakeRequest/_FakeAsyncClient/FakeDB convention as
test_infra_drafts_endpoint.py — call the router functions directly, no
TestClient, no real Postgres or AWS.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi import HTTPException

from src.routers import projects_router


class FakeRequest:
    def __init__(self, tenant_id="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant_id})()


class _FakeHttpResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.request = None

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(self.text, request=None, response=self)


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.last_call = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        self.last_call = ("POST", url, json)
        if self._exc:
            raise self._exc
        return self._response

    async def get(self, url, params=None):
        self.last_call = ("GET", url, params)
        if self._exc:
            raise self._exc
        return self._response


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeDB:
    def __init__(self, initial_row: dict | None):
        self.row = dict(initial_row) if initial_row is not None else None

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}

        if "SELECT * FROM infra_build_state" in sql:
            return _Result(dict(self.row) if self.row else None)

        if "SET status = :status, change_set_id" in sql:
            self.row["status"] = params["status"]
            self.row["change_set_id"] = params["change_set_id"]
            self.row["stack_name"] = params["stack_name"]
            self.row["stack_arn"] = params["stack_arn"]
            self.row["change_set_changes"] = json.loads(params["changes"])
            self.row["provisioning_error"] = params["error"]
            return _Result(None)

        if "SET status = 'INFRA_PROVISIONING'" in sql:
            self.row["status"] = "INFRA_PROVISIONING"
            return _Result(None)

        if "SET status = :status, provisioning_outputs" in sql:
            self.row["status"] = params["status"]
            self.row["provisioning_outputs"] = json.loads(params["outputs"])
            self.row["provisioning_error"] = params["error"]
            return _Result(None)

        return _Result(None)

    async def commit(self):
        pass


def _base_row(**overrides) -> dict:
    defaults = dict(
        draft_id="11111111-1111-1111-1111-111111111111",
        project_id=None,
        status="INFRA_APPROVED",
        intent_spec={"aws_region": "us-east-1"},
        archetype="web_service_with_database",
        infra_proposal={"cloudformation_template": '{"Resources": {}}'},
        readiness_outcome="auto_advance",
        readiness_reasons=[],
        error_message=None,
        cloud_provider="aws",
        change_set_id=None,
        stack_name=None,
        stack_arn=None,
        change_set_changes=[],
        provisioning_error=None,
        provisioning_outputs={},
        source="ai_created",
        existing_resources={},
        parent_draft_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return defaults


# ─────────────── create_infra_change_set ───────────────


def test_create_change_set_requires_infra_approved_status():
    db = FakeDB(_base_row(status="INFRA_PENDING_APPROVAL"))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.create_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))
    assert exc_info.value.status_code == 409


def test_create_change_set_404_for_unknown_draft():
    db = FakeDB(None)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.create_infra_change_set("nope", FakeRequest(), db=db))
    assert exc_info.value.status_code == 404


def test_create_change_set_missing_template_is_422():
    db = FakeDB(_base_row(infra_proposal={}))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.create_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))
    assert exc_info.value.status_code == 422


def test_create_change_set_ready_advances_to_change_set_ready(monkeypatch):
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(
            200,
            {
                "change_set_id": "cs-1", "stack_name": "smartcd-infra-11111111-1111-1111-1111-111111111111", "stack_id": "arn:x",
                "status": "READY", "status_reason": None,
                "changes": [{"action": "Add", "logical_id": "OrdersDb", "resource_type": "AWS::RDS::DBInstance"}],
            },
        )
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row())

    result = asyncio.run(projects_router.create_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))

    assert result["status"] == "INFRA_CHANGE_SET_READY"
    assert result["change_set_changes"][0]["logical_id"] == "OrdersDb"


def test_create_change_set_failed_marks_change_set_failed(monkeypatch):
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(
            200,
            {"change_set_id": "", "stack_name": "smartcd-infra-11111111-1111-1111-1111-111111111111", "stack_id": None,
             "status": "FAILED", "status_reason": "Template contains errors.", "changes": []},
        )
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row())

    result = asyncio.run(projects_router.create_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))

    assert result["status"] == "INFRA_CHANGE_SET_FAILED"
    assert result["provisioning_error"] == "Template contains errors."


def test_create_change_set_pipeline_worker_unreachable_is_502(monkeypatch):
    import httpx

    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.create_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))
    assert exc_info.value.status_code == 502


# ─────────────── execute_infra_change_set ───────────────


def test_execute_requires_change_set_ready_status():
    db = FakeDB(_base_row(status="INFRA_APPROVED"))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.execute_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))
    assert exc_info.value.status_code == 409


def test_execute_success_advances_to_provisioning(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, {"stack_id": "arn:x", "status": "UPDATE_IN_PROGRESS"}))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row(status="INFRA_CHANGE_SET_READY", change_set_id="cs-1", stack_name="smartcd-infra-11111111-1111-1111-1111-111111111111"))

    result = asyncio.run(projects_router.execute_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))

    assert result["status"] == "INFRA_PROVISIONING"


def test_execute_pipeline_worker_error_is_502(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(502, text="boto3 exploded"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row(status="INFRA_CHANGE_SET_READY", change_set_id="cs-1", stack_name="x"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.execute_infra_change_set("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))
    assert exc_info.value.status_code == 502


# ─────────────── get_infra_provisioning_status ───────────────


def test_status_returns_immediately_when_not_provisioning():
    db = FakeDB(_base_row(status="INFRA_PROVISIONED"))
    result = asyncio.run(projects_router.get_infra_provisioning_status("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))
    assert result["status"] == "INFRA_PROVISIONED"


def test_status_polls_and_updates_to_provisioned_on_success(monkeypatch):
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(
            200,
            {"status": "UPDATE_COMPLETE", "is_terminal": True, "succeeded": True,
             "outputs": {"DbEndpoint": "db.example.com"}, "status_reason": None},
        )
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row(status="INFRA_PROVISIONING", stack_name="smartcd-infra-11111111-1111-1111-1111-111111111111"))

    result = asyncio.run(projects_router.get_infra_provisioning_status("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))

    assert result["status"] == "INFRA_PROVISIONED"
    assert result["provisioning_outputs"]["DbEndpoint"] == "db.example.com"


def test_status_polls_and_updates_to_failed_on_failure(monkeypatch):
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(
            200,
            {"status": "UPDATE_ROLLBACK_COMPLETE", "is_terminal": True, "succeeded": False,
             "outputs": {}, "status_reason": "Resource creation failed."},
        )
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row(status="INFRA_PROVISIONING", stack_name="smartcd-infra-11111111-1111-1111-1111-111111111111"))

    result = asyncio.run(projects_router.get_infra_provisioning_status("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))

    assert result["status"] == "INFRA_PROVISIONING_FAILED"
    assert result["provisioning_error"] == "Resource creation failed."


def test_status_still_in_progress_leaves_status_unchanged(monkeypatch):
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(200, {"status": "UPDATE_IN_PROGRESS", "is_terminal": False, "succeeded": False, "outputs": {}, "status_reason": None})
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB(_base_row(status="INFRA_PROVISIONING", stack_name="smartcd-infra-11111111-1111-1111-1111-111111111111"))

    result = asyncio.run(projects_router.get_infra_provisioning_status("11111111-1111-1111-1111-111111111111", FakeRequest(), db=db))

    assert result["status"] == "INFRA_PROVISIONING"
