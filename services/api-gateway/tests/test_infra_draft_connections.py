"""
services/api-gateway/tests/test_infra_draft_connections.py

Backlog #3 - how an infra draft uses a tenant's AWS connection: it is resolved (and must be verified) before
anything is written, reaches EVERY provisioning call (discovery, describe-existing, change set, execute, status),
is inherited by edits, and never leaks into the model prompt. No connection = the platform's own account, and
those calls keep their original shape (GET status / GET discovery).
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from src.routers import projects_router
from src.routers.projects_router import InfraDraftEditRequest, InfraDraftRequest
from tests.test_infra_import_and_edit_endpoints import (
    DRAFT_ID, FakeDB, FakeRequest, PROPOSAL, VERIFIED, _Client, _Resp, _Result, _row,
)

CONN_ID = "22222222-2222-2222-2222-222222222222"
CONN = {"role_arn": "arn:aws:iam::123456789012:role/smartcd-platform-access", "external_id": "e" * 43, "default_region": "eu-north-1"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def connection_stub(monkeypatch):
    """load_provisioning_connection: CONN_ID is verified; anything else is unknown/unverified (a 422)."""
    seen = []

    async def load(db, tenant_id, connection_id):
        seen.append(connection_id)
        if not connection_id:
            return None
        if connection_id != CONN_ID:
            raise HTTPException(status_code=422, detail="Unknown AWS connection for this tenant.")
        return dict(CONN)

    monkeypatch.setattr(projects_router, "load_provisioning_connection", load)
    return seen


def spec(**over):
    base = dict(environment_tier="dev", archetype="web_service_with_database", aws_region="eu-north-1", needs_database=True)
    base.update(over)
    return InfraDraftRequest(**base)


# ─────────────── creating a draft ───────────────


def test_the_connection_is_stored_on_the_draft_and_never_sent_to_the_model(monkeypatch, connection_stub):
    client = _Client({"generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB()

    out = run(projects_router.create_infra_draft(spec(aws_connection_id=CONN_ID), FakeRequest(), db=db))

    assert out["aws_connection_id"] == CONN_ID
    sent = next(c for c in client.calls if "generate-infra" in c[1])[2]
    assert "aws_connection_id" not in sent["intent_spec"]
    assert "aws_connection_id" not in json.dumps(db.inserts[0]["intent_spec"])
    assert "e" * 43 not in json.dumps(sent)  # the ExternalId is nowhere near the prompt


def test_an_unknown_or_unverified_connection_is_rejected_before_anything_is_written(monkeypatch, connection_stub):
    client = _Client({"generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = FakeDB()

    with pytest.raises(HTTPException) as e:
        run(projects_router.create_infra_draft(spec(aws_connection_id="99999999-9999-9999-9999-999999999999"), FakeRequest(), db=db))

    assert e.value.status_code == 422 and db.inserts == [] and client.calls == []


def test_no_connection_is_the_platforms_own_account(monkeypatch, connection_stub):
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({"generate-infra": _Resp(200, PROPOSAL)}))
    out = run(projects_router.create_infra_draft(spec(), FakeRequest(), db=FakeDB()))
    assert out["aws_connection_id"] is None


def test_picking_existing_resources_verifies_them_inside_the_tenants_own_account(monkeypatch, connection_stub):
    client = _Client({"describe-existing": _Resp(200, VERIFIED), "generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    run(projects_router.create_infra_draft(
        spec(aws_connection_id=CONN_ID, source="existing", existing_selection={"database": "orders-db"}), FakeRequest(), db=FakeDB()))

    body = next(c for c in client.calls if "describe-existing" in c[1])[2]
    assert body["connection"] == CONN  # the customer's DB is looked up in the customer's account, not the platform's


# ─────────────── editing ───────────────


def test_an_edit_inherits_the_parents_connection(monkeypatch, connection_stub):
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({"generate-infra": _Resp(200, PROPOSAL)}))
    db = FakeDB(_row(aws_connection_id=CONN_ID))

    out = run(projects_router.edit_infra_draft(DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=db))

    assert out["aws_connection_id"] == CONN_ID


# ─────────────── provisioning calls ───────────────


class _Recorder(_Client):
    """Answers change-set / execute / status shapes and records the calls."""

    def __init__(self):
        super().__init__({
            "change-set": _Resp(200, {"change_set_id": "cs-1", "stack_name": "s", "stack_id": "a", "status": "READY", "status_reason": None, "changes": []}),
            "/infra-provisioning/execute": _Resp(200, {"stack_id": "a", "status": "UPDATE_IN_PROGRESS"}),
            "/infra-provisioning/status": _Resp(200, {"status": "UPDATE_IN_PROGRESS", "is_terminal": False, "succeeded": False, "outputs": {}, "status_reason": None}),
        })


class _ProvDB(FakeDB):
    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "SET status = :status, change_set_id" in sql or "SET status = 'INFRA_PROVISIONING'" in sql:
            return _Result(None)
        return await super().execute(stmt, params)


def test_change_set_and_execute_carry_the_connection(monkeypatch, connection_stub):
    client = _Recorder()
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    run(projects_router.create_infra_change_set(DRAFT_ID, FakeRequest(), db=_ProvDB(_row(aws_connection_id=CONN_ID))))
    run(projects_router.execute_infra_change_set(DRAFT_ID, FakeRequest(), db=_ProvDB(_row(
        status="INFRA_CHANGE_SET_READY", change_set_id="cs-1", stack_name="s", aws_connection_id=CONN_ID))))

    bodies = {c[1].rsplit("/", 1)[-1]: c[2] for c in client.calls if c[0] == "POST"}
    assert bodies["change-set"]["connection"] == CONN and bodies["execute"]["connection"] == CONN


def test_without_a_connection_the_change_set_carries_none(monkeypatch, connection_stub):
    client = _Recorder()
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    run(projects_router.create_infra_change_set(DRAFT_ID, FakeRequest(), db=_ProvDB(_row())))
    assert client.calls[0][2]["connection"] is None


def test_status_for_a_tenant_account_is_a_post_so_the_external_id_never_travels_in_a_url(monkeypatch, connection_stub):
    client = _Recorder()
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    run(projects_router.get_infra_provisioning_status(DRAFT_ID, FakeRequest(), db=_ProvDB(_row(
        status="INFRA_PROVISIONING", stack_name="s", aws_connection_id=CONN_ID))))

    method, url, body = client.calls[0]
    assert method == "POST" and url.endswith("/infra-provisioning/status") and body["connection"] == CONN
    assert "external" not in url


def test_status_for_the_platform_account_keeps_its_original_get(monkeypatch, connection_stub):
    client = _Recorder()
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    run(projects_router.get_infra_provisioning_status(DRAFT_ID, FakeRequest(), db=_ProvDB(_row(status="INFRA_PROVISIONING", stack_name="s"))))
    assert client.calls[0][0] == "GET"


def test_a_draft_whose_connection_is_no_longer_usable_fails_loudly_instead_of_using_the_platform_account(monkeypatch, connection_stub):
    client = _Recorder()
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    db = _ProvDB(_row(aws_connection_id="99999999-9999-9999-9999-999999999999"))

    with pytest.raises(HTTPException) as e:
        run(projects_router.create_infra_change_set(DRAFT_ID, FakeRequest(), db=db))

    assert e.value.status_code == 422 and client.calls == []  # nothing was sent anywhere


# ─────────────── discovery ───────────────


def test_discovery_in_a_tenant_account_posts_the_connection_and_defaults_to_its_region(monkeypatch, connection_stub):
    client = _Client({"discover-existing": _Resp(200, {"database": []})})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)

    run(projects_router.discover_existing_infra(FakeRequest(), "web_service_with_database", None, CONN_ID, db=FakeDB()))

    method, url, body = client.calls[0]
    assert method == "POST" and body["connection"] == CONN and body["region"] == "eu-north-1"


def test_discovery_without_a_connection_is_still_a_get_against_the_platform_account(monkeypatch, connection_stub):
    client = _Client({"discover-existing": _Resp(200, {"database": []})})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    run(projects_router.discover_existing_infra(FakeRequest(), "web_service_with_database", "us-east-1", None, db=FakeDB()))
    assert client.calls[0][0] == "GET" and client.calls[0][2] == {"archetype": "web_service_with_database", "region": "us-east-1"}
