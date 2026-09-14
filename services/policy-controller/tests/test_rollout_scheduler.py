"""
services/policy-controller/tests/test_rollout_scheduler.py

Real bug found live: rollout_scheduler.py's `graduate()` originally POSTed
to `{API_GATEWAY_URL}/api/v1/internal/pipeline-runs/{run_id}/graduate` —
missing the `/projects` segment the actual route
(projects_router.py::graduate_pipeline_run, mounted under
`/api/v1/projects`) lives at. The mismatched path fell outside
auth/middleware.py's `/api/v1/projects/internal/` allowlist too, so the
call failed with a 401 rather than 404 — caught only by watching it fail
live against the real running stack, not by any unit test, since the
mocked tests never exercise the real URL string. This asserts the exact
URL so a future refactor can't silently drift the two apart again.

Also covers the later real-graduation fix: `graduate()` now does the
Kubernetes work (actuation_executor.graduate_canary — baseline's image
genuinely swapped to canary's) BEFORE telling api-gateway to record the
new version, and skips the database call entirely if the cluster mutation
failed, so the tracked version never claims a state the cluster doesn't
actually have.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import httpx
import pytest

from src import rollout_scheduler

_TARGET = {
    "route_name": "svc-route",
    "namespace": "production",
    "canary_deployment_name": "svc-canary",
    "baseline_deployment_name": "svc-baseline",
    "tenant_id": "tenant-1",
}


def test_graduate_posts_to_the_real_projects_router_path(monkeypatch):
    captured = {}

    async def fake_graduate_canary(run_id, **kwargs):
        return {"status": "GRADUATED", "new_baseline_image": "registry/app:v2.0.0"}

    monkeypatch.setattr(rollout_scheduler, "graduate_canary", fake_graduate_canary)

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(rollout_scheduler.graduate("run-123", _TARGET, "v2.0.0"))

    assert result == "GRADUATED"
    assert captured["url"] == f"{rollout_scheduler.API_GATEWAY_URL}/api/v1/projects/internal/pipeline-runs/run-123/graduate"
    # The image actually read live off the canary Deployment is what gets
    # recorded — not blindly the requested target_version, which could
    # have drifted from what genuinely built and deployed.
    assert captured["json"] == {"tenant_id": "tenant-1", "new_version": "registry/app:v2.0.0"}


def test_graduate_skips_db_record_when_kubernetes_step_fails(monkeypatch):
    async def failing_graduate_canary(run_id, **kwargs):
        raise RuntimeError("kubernetes API unreachable")

    monkeypatch.setattr(rollout_scheduler, "graduate_canary", failing_graduate_canary)

    called = {"http": False}

    class ShouldNotBeCalledAsyncClient:
        def __init__(self, *args, **kwargs):
            called["http"] = True

    monkeypatch.setattr(httpx, "AsyncClient", ShouldNotBeCalledAsyncClient)

    result = asyncio.run(rollout_scheduler.graduate("run-123", _TARGET, "v2.0.0"))

    assert result == "GRADUATION_FAILED"
    assert called["http"] is False, "must not tell api-gateway a version is live when the cluster mutation failed"


def test_graduate_is_fail_soft_when_only_the_db_record_fails(monkeypatch):
    async def fake_graduate_canary(run_id, **kwargs):
        return {"status": "GRADUATED", "new_baseline_image": "registry/app:v2.0.0"}

    monkeypatch.setattr(rollout_scheduler, "graduate_canary", fake_graduate_canary)

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FailingAsyncClient)

    # The cluster is already correct at this point — only bookkeeping is
    # stale, so this still reports GRADUATED rather than a failure.
    result = asyncio.run(rollout_scheduler.graduate("run-123", _TARGET, "v2.0.0"))
    assert result == "GRADUATED"


def test_parse_duration_seconds_handles_seconds_and_minutes():
    assert rollout_scheduler.parse_duration_seconds("120s") == 120
    assert rollout_scheduler.parse_duration_seconds("5m") == 300


def test_parse_duration_seconds_returns_zero_for_malformed_input():
    assert rollout_scheduler.parse_duration_seconds("garbage") == 0
    assert rollout_scheduler.parse_duration_seconds("") == 0
    assert rollout_scheduler.parse_duration_seconds(None) == 0
