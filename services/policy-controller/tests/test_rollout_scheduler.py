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
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import httpx
import pytest

from src import rollout_scheduler


def test_graduate_posts_to_the_real_projects_router_path(monkeypatch):
    captured = {}

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

    asyncio.run(rollout_scheduler.graduate("run-123", "tenant-1", "v2.0.0"))

    assert captured["url"] == f"{rollout_scheduler.API_GATEWAY_URL}/api/v1/projects/internal/pipeline-runs/run-123/graduate"
    assert captured["json"] == {"tenant_id": "tenant-1", "new_version": "v2.0.0"}


def test_graduate_is_fail_soft_on_http_error(monkeypatch):
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

    # Must not raise — a failed graduation shouldn't crash the caller
    # (the traffic promotion that already happened stands either way).
    asyncio.run(rollout_scheduler.graduate("run-123", "tenant-1", "v2.0.0"))


def test_parse_duration_seconds_handles_seconds_and_minutes():
    assert rollout_scheduler.parse_duration_seconds("120s") == 120
    assert rollout_scheduler.parse_duration_seconds("5m") == 300


def test_parse_duration_seconds_returns_zero_for_malformed_input():
    assert rollout_scheduler.parse_duration_seconds("garbage") == 0
    assert rollout_scheduler.parse_duration_seconds("") == 0
    assert rollout_scheduler.parse_duration_seconds(None) == 0
