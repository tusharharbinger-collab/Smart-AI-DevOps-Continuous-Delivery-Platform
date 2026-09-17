"""
services/api-gateway/tests/test_webhook_registration_endpoint.py

Covers register_project_webhook (projects_router.py) — the endpoint that
makes "git push -> cloud" require zero manual GitHub UI steps by actually
creating the repo's webhook subscription via the GitHub API, using the
signed-in user's own OAuth token (the already-requested `repo` scope
covers repository webhooks per GitHub's own scope docs). Mocks the GitHub
API with httpx.MockTransport (never touches real GitHub) and asserts both
the idempotent "already registered" path and the real-create path, plus
that the Redis repo->project mapping gets (re)written either way.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from src.routers import projects_router


class FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)


class FakeApp:
    def __init__(self, redis_client):
        self.state = type("S", (), {"redis": redis_client})()


class FakeRequest:
    def __init__(self, redis_client, user_id="user-1"):
        self.app = FakeApp(redis_client)
        self.state = type("S", (), {"user_id": user_id})()
        self.headers = {}


PROJECT = {"pipeline_id": "pipe-1", "repo_url": "https://github.com/octocat/hello-world.git", "branch": "main"}

# Captured once, before any test monkeypatches httpx.AsyncClient itself —
# a lambda that referenced httpx.AsyncClient (the patched name) instead of
# this real class would recurse into itself infinitely.
_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    return lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"})


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(projects_router.settings, "GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(projects_router.settings, "API_GATEWAY_PUBLIC_URL", "https://smartcd.example.com")

    async def fake_load_project(db, project_id, tenant_id):
        return dict(PROJECT)

    async def fake_resolve_token(request, header_token):
        return "gho_faketoken"

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_resolve_token", fake_resolve_token)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")


def test_503_when_webhook_secret_not_configured(monkeypatch):
    monkeypatch.setattr(projects_router.settings, "GITHUB_WEBHOOK_SECRET", "")
    redis_client = FakeRedis()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.register_project_webhook("proj-1", FakeRequest(redis_client), db=object())
        )
    assert exc_info.value.status_code == 503


def test_409_when_project_has_no_github_repo(monkeypatch):
    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1", "repo_url": None, "branch": "main"}

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    redis_client = FakeRedis()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.register_project_webhook("proj-1", FakeRequest(redis_client), db=object())
        )
    assert exc_info.value.status_code == 409


def test_creates_a_new_hook_when_none_exists(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/hooks"):
            body = json.loads(request.content)
            assert body["events"] == ["push"]
            assert body["config"]["url"] == "https://smartcd.example.com/api/v1/webhooks/github"
            assert body["config"]["secret"] == "test-secret"
            return httpx.Response(201, json={"id": 999})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    redis_client = FakeRedis()
    result = asyncio.run(
        projects_router.register_project_webhook("proj-1", FakeRequest(redis_client), db=object())
    )

    assert result["created"] is True
    assert result["hook_id"] == 999
    assert result["repo"] == "octocat/hello-world"
    assert any(m == "POST" for m, _ in calls)

    mapping = asyncio.run(projects_router.webhook_registry.resolve(redis_client, "octocat/hello-world"))
    assert mapping == {"project_id": "proj-1", "tenant_id": "tenant-1", "branch": "main"}


def test_reuses_an_existing_hook_instead_of_duplicating(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(
                200,
                json=[
                    {"id": 111, "config": {"url": "https://smartcd.example.com/api/v1/webhooks/github"}},
                ],
            )
        if request.method == "POST":
            raise AssertionError("must not create a duplicate hook when one already points at our URL")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    redis_client = FakeRedis()
    result = asyncio.run(
        projects_router.register_project_webhook("proj-1", FakeRequest(redis_client), db=object())
    )

    assert result["created"] is False
    assert result["hook_id"] == 111


def test_502_when_github_rejects_hook_creation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed"})
        raise AssertionError("unexpected request")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    redis_client = FakeRedis()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.register_project_webhook("proj-1", FakeRequest(redis_client), db=object())
        )
    assert exc_info.value.status_code == 502
