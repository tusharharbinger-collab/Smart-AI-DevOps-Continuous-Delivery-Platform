"""
services/api-gateway/tests/test_infra_drafts_endpoint.py

Covers create_infra_draft/get_infra_draft/approve_infra_draft
(projects_router.py) — Phase 4's state-machine endpoints. Same convention
as test_project_chatops.py: call the router functions directly with a
FakeRequest (tenant_id via .state) and a monkeypatched httpx client for the
explainability-service call, plus an in-memory FakeDB that mimics the
INSERT → generate → UPDATE → SELECT sequence create_infra_draft performs.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import pytest
from fastapi import HTTPException

from src.routers import projects_router
from shared.intent_spec import EnvironmentTier, IntentSpec


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


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        if self._exc:
            raise self._exc
        return self._response


class FakeDB:
    """In-memory stand-in for infra_build_state, keyed by draft_id."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.committed = False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}

        if "INSERT INTO infra_build_state" in sql:
            self.rows[params["draft_id"]] = {
                "draft_id": params["draft_id"],
                "tenant_id": params["tenant_id"],
                "project_id": None,
                "status": "INFRA_DRAFTING",
                "intent_spec": json.loads(params["intent_spec"]),
                "archetype": params["archetype"],
                "infra_proposal": None,
                "readiness_outcome": None,
                "readiness_reasons": [],
                "error_message": None,
                # Phase 7 columns (migration 0017) — not exercised by these
                # Phase 4 tests, but _infra_draft_row_to_dict now reads them
                # unconditionally, so a real row always has them present.
                "cloud_provider": "aws",
                "change_set_id": None,
                "stack_name": None,
                "stack_arn": None,
                "change_set_changes": [],
                "provisioning_error": None,
                "provisioning_outputs": {},
                # Migration 0018 (AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phase A).
                "source": params.get("source", "ai_created"),
                "existing_resources": json.loads(params["existing"]) if params.get("existing") else {},
                "parent_draft_id": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            return _Result(None)

        if "SET status = 'INFRA_DRAFT_FAILED'" in sql:
            row = self.rows[params["draft_id"]]
            row["status"] = "INFRA_DRAFT_FAILED"
            row["error_message"] = params["err"]
            return _Result(None)

        if "SET status = :status, infra_proposal" in sql:
            row = self.rows[params["draft_id"]]
            row["status"] = params["status"]
            row["infra_proposal"] = json.loads(params["proposal"])
            row["readiness_outcome"] = params["outcome"]
            row["readiness_reasons"] = json.loads(params["reasons"])
            return _Result(None)

        if "SET status = 'INFRA_APPROVED'" in sql:
            row = self.rows[params["draft_id"]]
            row["status"] = "INFRA_APPROVED"
            return _Result(None)

        if "SELECT * FROM infra_build_state" in sql:
            return _Result(self.rows.get(params["draft_id"]))

        return _Result(None)

    async def commit(self):
        self.committed = True


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


def _spec(**overrides) -> IntentSpec:
    defaults = dict(environment_tier=EnvironmentTier.DEV, archetype="stateless_web_service", needs_database=True)
    defaults.update(overrides)
    return IntentSpec(**defaults)


def _proposal_payload() -> dict:
    return {
        "name": "svc-infra",
        "topology": {"nodes": [{"id": "alb", "type": "alb", "label": "ALB"}], "edges": []},
        "iac_terraform": "resource \"aws_lb\" \"x\" {}\n",
        "estimated_monthly_cost_usd": 20.0,
        "cost_breakdown": [{"resource": "ALB", "monthly_usd": 20.0}],
        "policy_checks": [{"check": "encryption_at_rest", "passed": True, "detail": "n/a"}],
    }


def test_missing_archetype_is_rejected_before_any_db_write():
    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.create_infra_draft(
                _spec(archetype=None), FakeRequest(), db=db
            )
        )
    assert exc_info.value.status_code == 422
    assert db.rows == {}


def test_auto_advance_readiness_skips_straight_to_infra_approved(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, _proposal_payload()))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    db = FakeDB()
    result = asyncio.run(
        projects_router.create_infra_draft(
            _spec(archetype="stateless_web_service", detected_confidence="high", monthly_budget_usd=20),
            FakeRequest(),
            db=db,
        )
    )

    assert result["status"] == "INFRA_APPROVED"
    assert result["readiness_outcome"] == "auto_advance"
    assert result["infra_proposal"]["name"] == "svc-infra"
    # No explicit commit — auth/middleware.py's session.begin() commits the
    # whole request automatically; a handler-level db.commit() would end
    # the transaction early (the real bug found testing this live).
    assert db.committed is False


def test_require_approval_readiness_pauses_at_pending_approval(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, _proposal_payload()))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    db = FakeDB()
    result = asyncio.run(
        projects_router.create_infra_draft(
            _spec(archetype="multi_service", detected_confidence="high"),
            FakeRequest(),
            db=db,
        )
    )

    assert result["status"] == "INFRA_PENDING_APPROVAL"
    assert result["readiness_outcome"] == "require_approval"


def test_explainability_service_failure_raises_502_with_the_real_error(monkeypatch):
    # Real fix found live (testing against actual Postgres): the handler
    # must never call db.commit() itself — auth/middleware.py wraps the
    # whole request in one transaction and commits it automatically on
    # success or rolls the whole thing back on any exception. That means a
    # Groq failure here rolls back the INSERT too — no INFRA_DRAFT_FAILED
    # row survives — so this only asserts the HTTPException carries the
    # real error, not a persisted row (which this FakeDB can't model
    # transaction rollback for anyway).
    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.create_infra_draft(_spec(), FakeRequest(), db=db))

    assert exc_info.value.status_code == 502
    assert "connection refused" in exc_info.value.detail


def test_explainability_service_error_status_raises_502_with_the_real_error(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(502, text="AI infra generation failed: timeout"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.create_infra_draft(_spec(), FakeRequest(), db=db))

    assert exc_info.value.status_code == 502
    assert "timeout" in exc_info.value.detail


def test_get_infra_draft_returns_404_for_unknown_draft():
    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.get_infra_draft(str(uuid.uuid4()), FakeRequest(), db=db))
    assert exc_info.value.status_code == 404


def test_get_infra_draft_returns_404_not_500_for_a_malformed_id():
    # Real bug found live: WHERE draft_id = :draft_id against a UUID column
    # raised a raw asyncpg DataError (surfaced as an opaque 500) for a
    # non-UUID path parameter instead of the clean 404 a "not found" should
    # look like — a malformed id and a well-formed-but-absent one must be
    # indistinguishable to the caller.
    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.get_infra_draft("not-a-real-uuid", FakeRequest(), db=db))
    assert exc_info.value.status_code == 404
    # And crucially: no DB query was even attempted for a malformed id.
    assert db.rows == {}


def test_get_infra_draft_returns_the_row(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, _proposal_payload()))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB()
    created = asyncio.run(projects_router.create_infra_draft(_spec(), FakeRequest(), db=db))

    fetched = asyncio.run(projects_router.get_infra_draft(created["draft_id"], FakeRequest(), db=db))
    assert fetched["draft_id"] == created["draft_id"]


def test_approve_pending_draft_moves_to_infra_approved(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, _proposal_payload()))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB()
    created = asyncio.run(
        projects_router.create_infra_draft(
            _spec(archetype="multi_service"), FakeRequest(), db=db
        )
    )
    assert created["status"] == "INFRA_PENDING_APPROVAL"

    approved = asyncio.run(projects_router.approve_infra_draft(created["draft_id"], FakeRequest(), db=db))
    assert approved["status"] == "INFRA_APPROVED"


def test_approving_a_draft_not_pending_approval_is_rejected(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, _proposal_payload()))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)
    db = FakeDB()
    created = asyncio.run(
        projects_router.create_infra_draft(
            _spec(archetype="stateless_web_service", detected_confidence="high", monthly_budget_usd=10),
            FakeRequest(),
            db=db,
        )
    )
    assert created["status"] == "INFRA_APPROVED"  # already auto-advanced

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.approve_infra_draft(created["draft_id"], FakeRequest(), db=db))
    assert exc_info.value.status_code == 409


def test_approve_returns_404_for_unknown_draft():
    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.approve_infra_draft("does-not-exist", FakeRequest(), db=db))
    assert exc_info.value.status_code == 404
