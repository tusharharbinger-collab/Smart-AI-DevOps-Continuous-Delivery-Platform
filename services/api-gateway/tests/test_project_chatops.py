"""
services/api-gateway/tests/test_project_chatops.py

Covers ask_project_question (projects_router.py) — the ChatOps query
interface's api-gateway side. Same test convention as
test_cost_history_endpoint.py: call the router function directly with
monkeypatched `_load_project`/`_get_tenant_id` and a fake DB, since this
endpoint's own job is thin — auth/tenant scoping, then a proxy call to
explainability-service (mocked here the same way
test_chatops_answerer.py mocks the Groq call one layer down).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from src.routers import projects_router
from src.routers.projects_router import AskProjectQuestionRequest


class FakeRequest:
    def __init__(self, tenant_id="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant_id})()


class _FakeHttpResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.last_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        self.last_json = json
        if self._exc:
            raise self._exc
        return self._response


def _patch_project_and_tenant(monkeypatch, tenant_id="tenant-1"):
    async def fake_load_project(db, project_id, tid):
        assert tid == tenant_id
        return {"pipeline_id": "pipe-1", "project_id": project_id}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: tenant_id)


def test_proxies_the_question_with_the_sessions_own_tenant_id_never_the_body(monkeypatch):
    _patch_project_and_tenant(monkeypatch, tenant_id="tenant-1")
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(200, {"answer": "Run abc123 rolled back.", "cited_run_ids": ["run-abc123"], "confidence": "grounded"})
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        projects_router.ask_project_question(
            "proj-1", AskProjectQuestionRequest(question="why did it roll back?"), FakeRequest(), db="fake-db"
        )
    )

    assert result["answer"] == "Run abc123 rolled back."
    assert fake_client.last_json == {"tenant_id": "tenant-1", "project_id": "proj-1", "question": "why did it roll back?"}


def test_404s_for_a_project_outside_the_callers_tenant(monkeypatch):
    async def fake_load_project_not_found(db, project_id, tenant_id):
        raise HTTPException(status_code=404, detail="Project not found")

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project_not_found)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.ask_project_question(
                "someone-elses-project", AskProjectQuestionRequest(question="anything"), FakeRequest(), db="fake-db"
            )
        )
    assert exc_info.value.status_code == 404


def test_returns_502_when_explainability_service_is_unreachable(monkeypatch):
    _patch_project_and_tenant(monkeypatch)
    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.ask_project_question(
                "proj-1", AskProjectQuestionRequest(question="anything"), FakeRequest(), db="fake-db"
            )
        )
    assert exc_info.value.status_code == 502
