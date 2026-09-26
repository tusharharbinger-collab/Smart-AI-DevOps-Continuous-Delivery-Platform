"""
services/api-gateway/tests/test_infra_import_and_edit_endpoints.py

AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md — the "existing vs AI-created" source choice
(create_infra_draft with source='existing', discover-existing) and prompt-driven edits
(edit_infra_draft). Same call-the-router-function-directly convention as
test_infra_drafts_endpoint.py; no TestClient, Postgres, AWS or Groq.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import pytest
from fastapi import HTTPException

from src.routers import projects_router
from src.routers.projects_router import InfraDraftEditRequest, InfraDraftRequest

DRAFT_ID = "11111111-1111-1111-1111-111111111111"
PROPOSAL = {"name": "x", "topology": {"nodes": [], "edges": []}, "cloudformation_template": "{}"}
VERIFIED = {"database": {"id": "orders-db", "details": {"engine": "postgres"}}}


class FakeRequest:
    def __init__(self):
        self.state = type("S", (), {"tenant_id": "tenant-1"})()


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code, self._payload, self.text, self.request = status_code, payload, text, None

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(self.text, request=None, response=self)


class _Client:
    """Routes by URL substring; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _respond(self, url):
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected URL {url}")

    async def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        return self._respond(url)

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))
        return self._respond(url)


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeDB:
    def __init__(self, parent=None):
        self.rows = {DRAFT_ID: parent} if parent else {}
        self.inserts = []

    async def execute(self, stmt, params=None):
        sql, params = str(stmt), params or {}
        if "INSERT INTO infra_build_state" in sql:
            self.inserts.append(params)
            new_id = params.get("new_id") or params["draft_id"]
            self.rows[new_id] = _row(
                draft_id=new_id, status=params.get("status", "INFRA_DRAFTING"),
                intent_spec=json.loads(params["intent_spec"]), archetype=params["archetype"],
                infra_proposal=json.loads(params["proposal"]) if params.get("proposal") else None,
                source=params.get("source", "ai_created"),
                existing_resources=json.loads(params["existing"]) if params.get("existing") else {},
                parent_draft_id=params.get("parent_id"), stack_name=params.get("stack_name"),
                project_id=params.get("project_id"), aws_connection_id=params.get("conn"),
            )
        elif "SET status = :status, infra_proposal" in sql:
            row = self.rows[params["draft_id"]]
            row.update(status=params["status"], infra_proposal=json.loads(params["proposal"]),
                       readiness_outcome=params["outcome"], readiness_reasons=json.loads(params["reasons"]))
        elif "SELECT * FROM infra_build_state" in sql:
            return _Result(self.rows.get(params["draft_id"]))
        return _Result(None)


def _row(**over):
    base = dict(
        draft_id=DRAFT_ID, project_id=None, status="INFRA_APPROVED",
        intent_spec={"environment_tier": "dev", "archetype": "web_service_with_database", "aws_region": "us-east-1"},
        archetype="web_service_with_database", infra_proposal=PROPOSAL, readiness_outcome="auto_advance",
        readiness_reasons=[], error_message=None, cloud_provider="aws", change_set_id=None, stack_name=None,
        stack_arn=None, change_set_changes=[], provisioning_error=None, provisioning_outputs={},
        source="ai_created", existing_resources={}, parent_draft_id=None, aws_connection_id=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return base


def _spec(**over):
    base = dict(environment_tier="dev", archetype="web_service_with_database", aws_region="us-east-1")
    base.update(over)
    return InfraDraftRequest(**base)


# ─────────── create with source ───────────


def test_existing_source_requires_a_selection():
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.create_infra_draft(_spec(source="existing"), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 422


def test_ai_created_source_rejects_a_stray_selection():
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.create_infra_draft(
            _spec(existing_selection={"database": "orders-db"}), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 422


def test_unknown_source_is_rejected():
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.create_infra_draft(_spec(source="bogus"), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 422


def test_existing_source_verifies_picks_and_hands_ground_truth_to_the_agent(monkeypatch):
    client = _Client({
        "describe-existing": _Resp(200, VERIFIED),
        "generate-infra": _Resp(200, PROPOSAL),
    })
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB()

    result = asyncio.run(projects_router.create_infra_draft(
        _spec(source="existing", existing_selection={"database": "orders-db"}), FakeRequest(), db=db))

    assert result["source"] == "existing"
    assert result["existing_resources"] == VERIFIED
    gen_call = next(c for c in client.calls if "generate-infra" in c[1])
    assert gen_call[2]["existing_resources"] == VERIFIED
    # The two routing-only fields never reach the stored spec or the model.
    assert "source" not in gen_call[2]["intent_spec"] and "existing_selection" not in gen_call[2]["intent_spec"]


def test_a_pick_aws_cannot_confirm_is_rejected_before_any_db_write(monkeypatch):
    client = _Client({"describe-existing": _Resp(422, {"detail": "No existing database named 'ghost' found in us-east-1."})})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB()

    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.create_infra_draft(
            _spec(source="existing", existing_selection={"database": "ghost"}), FakeRequest(), db=db))

    assert e.value.status_code == 422 and "ghost" in e.value.detail
    assert db.inserts == []


# ─────────── discovery ───────────


def test_discover_existing_proxies_pipeline_worker(monkeypatch):
    client = _Client({"discover-existing": _Resp(200, {"database": [{"id": "orders-db"}]})})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    out = asyncio.run(projects_router.discover_existing_infra(FakeRequest(), "web_service_with_database", "us-east-1", None, db=FakeDB()))

    assert out["database"][0]["id"] == "orders-db"
    assert client.calls[0][2] == {"archetype": "web_service_with_database", "region": "us-east-1"}


def test_discover_existing_route_is_registered_before_the_draft_id_route():
    paths = [r.path for r in projects_router.router.routes if getattr(r, "methods", None) and "GET" in r.methods]
    assert paths.index("/infra-drafts/discover-existing") < paths.index("/infra-drafts/{draft_id}")


# ─────────── edit ───────────


def test_edit_creates_a_new_linked_draft_and_never_overwrites_the_parent(monkeypatch):
    edited = {**PROPOSAL, "name": "edited"}
    client = _Client({"generate-infra": _Resp(200, edited)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB(_row(source="existing", existing_resources=VERIFIED))

    out = asyncio.run(projects_router.edit_infra_draft(
        DRAFT_ID, InfraDraftEditRequest(instruction="add a redis cache"), FakeRequest(), db=db))

    assert out["draft_id"] != DRAFT_ID
    assert out["parent_draft_id"] == DRAFT_ID
    assert out["infra_proposal"]["name"] == "edited"
    assert out["source"] == "existing" and out["existing_resources"] == VERIFIED
    assert db.rows[DRAFT_ID]["infra_proposal"] == PROPOSAL  # parent untouched
    payload = client.calls[0][2]
    assert payload["edit"]["instruction"] == "add a redis cache"
    assert payload["edit"]["current_proposal"] == PROPOSAL
    assert payload["existing_resources"] == VERIFIED


def test_edit_re_enters_the_approval_gate_never_auto_applies(monkeypatch):
    client = _Client({"generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    # multi_service always requires approval (deployment_readiness.py).
    db = FakeDB(_row(archetype="multi_service",
                     intent_spec={"environment_tier": "dev", "archetype": "multi_service", "aws_region": "us-east-1"}))

    out = asyncio.run(projects_router.edit_infra_draft(
        DRAFT_ID, InfraDraftEditRequest(instruction="make it bigger"), FakeRequest(), db=db))

    assert out["status"] == "INFRA_PENDING_APPROVAL"


def test_editing_a_provisioned_draft_inherits_its_stack_so_the_change_set_is_an_update(monkeypatch):
    client = _Client({"generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB(_row(status="INFRA_PROVISIONED", stack_name="smartcd-infra-parent"))

    out = asyncio.run(projects_router.edit_infra_draft(
        DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=db))

    assert out["stack_name"] == "smartcd-infra-parent"


def test_editing_an_unprovisioned_draft_does_not_inherit_a_stack(monkeypatch):
    client = _Client({"generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB(_row(status="INFRA_CHANGE_SET_READY", stack_name="smartcd-infra-parent"))

    out = asyncio.run(projects_router.edit_infra_draft(
        DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=db))

    assert out["stack_name"] is None


@pytest.mark.parametrize("status", ["INFRA_PROVISIONING", "INFRA_DRAFTING", "INFRA_CHANGE_SET_CREATING"])
def test_editing_mid_flight_is_rejected(status):
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.edit_infra_draft(
            DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=FakeDB(_row(status=status))))
    assert e.value.status_code == 409


def test_edit_of_unknown_or_malformed_draft():
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.edit_infra_draft(
            DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.edit_infra_draft(
            "nope", InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 404


def test_edit_surfaces_agent_failure_as_502(monkeypatch):
    client = _Client({"generate-infra": _Resp(502, text="edit removed OrdersDb")})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.edit_infra_draft(
            DRAFT_ID, InfraDraftEditRequest(instruction="drop the db"), FakeRequest(), db=FakeDB(_row())))
    assert e.value.status_code == 502 and "OrdersDb" in e.value.detail


# ─────────── change set threading ───────────


def test_change_set_for_an_existing_source_draft_requests_import(monkeypatch):
    client = _Client({"change-set": _Resp(200, {
        "change_set_id": "cs-1", "stack_name": "s", "stack_id": "a", "status": "READY",
        "status_reason": None, "changes": [{"action": "Import", "logical_id": "OrdersDb", "resource_type": "AWS::RDS::DBInstance"}]})})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    class DB(FakeDB):
        async def execute(self, stmt, params=None):
            if "SET status = :status, change_set_id" in str(stmt):
                return _Result(None)
            return await super().execute(stmt, params)

    db = DB(_row(source="existing", stack_name="smartcd-infra-parent"))
    asyncio.run(projects_router.create_infra_change_set(DRAFT_ID, FakeRequest(), db=db))

    sent = client.calls[0][2]
    assert sent["import_existing"] is True
    assert sent["stack_name"] == "smartcd-infra-parent"


def test_change_set_for_an_ai_created_draft_does_not_request_import(monkeypatch):
    client = _Client({"change-set": _Resp(200, {
        "change_set_id": "cs-1", "stack_name": "s", "stack_id": "a", "status": "READY", "status_reason": None, "changes": []})})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    class DB(FakeDB):
        async def execute(self, stmt, params=None):
            if "SET status = :status, change_set_id" in str(stmt):
                return _Result(None)
            return await super().execute(stmt, params)

    asyncio.run(projects_router.create_infra_change_set(DRAFT_ID, FakeRequest(), db=DB(_row())))

    assert client.calls[0][2]["import_existing"] is False


def test_a_generation_timeout_is_a_readable_502_not_an_empty_message(monkeypatch):
    # Found live: str(httpx.ReadTimeout(...)) is "", so the user saw "AI infra generation unavailable: ".
    class _Timeout(_Client):
        async def post(self, url, json=None, **kw):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Timeout({}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.edit_infra_draft(
            DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=FakeDB(_row())))
    assert e.value.status_code == 502 and "ReadTimeout" in e.value.detail
