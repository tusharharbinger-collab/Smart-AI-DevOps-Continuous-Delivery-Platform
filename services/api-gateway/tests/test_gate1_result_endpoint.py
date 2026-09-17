"""
services/api-gateway/tests/test_gate1_result_endpoint.py

Covers report_gate1_result (projects_router.py) — the internal endpoint
pipeline-worker's gate1 consumer calls back into once a webhook-triggered
push's real build+test dry run finishes. On passed=True, this is the ONE
place that actually calls _trigger_rollout_internal for a webhook-
originated push (the webhook receiver itself only ever queues the check —
see test_github_webhook.py); on passed=False, nothing about the real
pipeline/cluster is ever touched, matching the "fail here -> no deployment
attempt at all" requirement.
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

_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    return lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"})


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
    def __init__(self, redis_client):
        self.app = FakeApp(redis_client)


class FakeDB:
    def __init__(self):
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return None

    async def commit(self):
        pass


def test_422_when_tenant_id_missing():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.report_gate1_result(
                "proj-1", {"passed": True}, FakeRequest(FakeRedis()), db=FakeDB()
            )
        )
    assert exc_info.value.status_code == 422


def test_422_when_passed_missing():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.report_gate1_result(
                "proj-1", {"tenant_id": "tenant-1"}, FakeRequest(FakeRedis()), db=FakeDB()
            )
        )
    assert exc_info.value.status_code == 422


def test_blocked_result_never_touches_the_real_pipeline(monkeypatch):
    called = []

    async def exploding_trigger(**kwargs):
        called.append(kwargs)
        raise AssertionError("must never trigger a real rollout on a failed gate1 check")

    monkeypatch.setattr(projects_router, "_trigger_rollout_internal", exploding_trigger)
    redis_client = FakeRedis()

    result = asyncio.run(
        projects_router.report_gate1_result(
            "proj-1",
            {
                "tenant_id": "tenant-1", "passed": False, "commit_sha": "abc123",
                "commit_message": "broken commit", "stage": "build", "error": "COPY failed: file not found",
                "human_side": True,
            },
            FakeRequest(redis_client),
            db=FakeDB(),
        )
    )

    assert result == {"status": "BLOCKED", "project_id": "proj-1"}
    assert called == []

    stored = json.loads(asyncio.run(redis_client.get("gate1_last_result:proj-1")))
    assert stored["passed"] is False
    assert stored["stage"] == "build"
    assert stored["human_side"] is True
    assert stored["commit_sha"] == "abc123"


def test_passed_result_triggers_the_real_rollout(monkeypatch):
    captured = {}

    async def fake_trigger(**kwargs):
        captured.update(kwargs)
        return "run-real-123"

    async def fake_load_project(db, project_id, tenant_id):
        assert project_id == "proj-1"
        assert tenant_id == "tenant-1"
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_trigger_rollout_internal", fake_trigger)
    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    redis_client = FakeRedis()

    result = asyncio.run(
        projects_router.report_gate1_result(
            "proj-1",
            {"tenant_id": "tenant-1", "passed": True, "commit_sha": "deadbeef", "commit_message": "fix: real bug"},
            FakeRequest(redis_client),
            db=FakeDB(),
        )
    )

    assert result == {"status": "TRIGGERED", "project_id": "proj-1", "pipeline_run_id": "run-real-123"}
    assert captured["trigger_type"] == "GITHUB_PUSH"
    assert captured["commit_sha"] == "deadbeef"
    assert captured["commit_message"] == "fix: real bug"
    assert captured["tenant_id"] == "tenant-1"

    stored = json.loads(asyncio.run(redis_client.get("gate1_last_result:proj-1")))
    # test_warning (non-blocking test stage, see test_stage_non_blocking
    # coverage in pipeline-worker) — visible even when the gate passes,
    # since a test command's own failure never stops the rollout this
    # branch just triggered; None here since the body's own test_warning
    # was never set by this test's request payload.
    assert stored == {
        "passed": True, "commit_sha": "deadbeef", "pipeline_run_id": "run-real-123", "test_warning": None,
    }


def test_passed_result_still_surfaces_a_non_blocking_test_warning(monkeypatch):
    """A failing test command must never stop the rollout on a passed gate1
    check (build succeeded) — but the failure itself must still be visible,
    not silently dropped, once it's recorded alongside the passed result."""
    async def fake_trigger(**kwargs):
        return "run-real-456"

    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1"}

    monkeypatch.setattr(projects_router, "_trigger_rollout_internal", fake_trigger)
    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    redis_client = FakeRedis()

    result = asyncio.run(
        projects_router.report_gate1_result(
            "proj-1",
            {
                "tenant_id": "tenant-1", "passed": True, "commit_sha": "deadbeef",
                "commit_message": "fix: real bug", "test_warning": "Tests failed: 1 failed, 3 passed",
            },
            FakeRequest(redis_client),
            db=FakeDB(),
        )
    )

    assert result == {"status": "TRIGGERED", "project_id": "proj-1", "pipeline_run_id": "run-real-456"}
    stored = json.loads(asyncio.run(redis_client.get("gate1_last_result:proj-1")))
    assert stored["test_warning"] == "Tests failed: 1 failed, 3 passed"


def test_blocked_result_fetches_real_logs_and_requests_a_grounded_rca(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + str(request.url))
        if str(request.url).endswith("/build-preview/gate1-check-42/logs"):
            return httpx.Response(200, json={"lines": ["Cloning...", "Build FAILED: COPY failed"]})
        if str(request.url).endswith("/stage-failure-rca"):
            body = json.loads(request.content)
            assert body["run_id"] == "gate1-check-42"
            assert body["failed_stage"] == "build"
            assert body["recent_logs"] == ["Cloning...", "Build FAILED: COPY failed"]
            return httpx.Response(
                200,
                json={
                    "likely_cause": "The Dockerfile COPYs a file that doesn't exist in the repo.",
                    "evidence": ["Build FAILED: COPY failed"],
                    "suggested_fix": "Check the COPY path matches a real file in the repo.",
                },
            )
        raise AssertionError(f"unexpected request to {request.url}")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    redis_client = FakeRedis()

    result = asyncio.run(
        projects_router.report_gate1_result(
            "proj-1",
            {
                "tenant_id": "tenant-1", "passed": False, "commit_sha": "abc123",
                "stage": "build", "error": "COPY failed: file not found",
                "gate1_check_id": "gate1-check-42",
            },
            FakeRequest(redis_client),
            db=FakeDB(),
        )
    )

    assert result == {"status": "BLOCKED", "project_id": "proj-1"}
    stored = json.loads(asyncio.run(redis_client.get("gate1_last_result:proj-1")))
    assert stored["rca"]["likely_cause"] == "The Dockerfile COPYs a file that doesn't exist in the repo."
    assert stored["rca"]["suggested_fix"] == "Check the COPY path matches a real file in the repo."
    assert any("/build-preview/gate1-check-42/logs" in c for c in calls)
    assert any("/stage-failure-rca" in c for c in calls)


def test_blocked_result_is_still_recorded_when_the_rca_request_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="explainability-service unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    redis_client = FakeRedis()

    result = asyncio.run(
        projects_router.report_gate1_result(
            "proj-1",
            {
                "tenant_id": "tenant-1", "passed": False, "commit_sha": "abc123",
                "stage": "build", "error": "COPY failed", "gate1_check_id": "gate1-check-99",
            },
            FakeRequest(redis_client),
            db=FakeDB(),
        )
    )

    assert result == {"status": "BLOCKED", "project_id": "proj-1"}
    stored = json.loads(asyncio.run(redis_client.get("gate1_last_result:proj-1")))
    assert stored["rca"] is None
    assert stored["passed"] is False


def test_no_rca_request_made_when_gate1_check_id_is_absent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        httpx, "AsyncClient", _mock_async_client(lambda req: calls.append(req) or httpx.Response(200, json={})),
    )
    redis_client = FakeRedis()

    asyncio.run(
        projects_router.report_gate1_result(
            "proj-1",
            {"tenant_id": "tenant-1", "passed": False, "stage": "build", "error": "x"},
            FakeRequest(redis_client),
            db=FakeDB(),
        )
    )

    assert calls == []
    stored = json.loads(asyncio.run(redis_client.get("gate1_last_result:proj-1")))
    assert stored["rca"] is None
