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
# src/actuation_executor.py -> `from shared import eks_auth` needs the repo
# root on sys.path too (shared/ is a repo-root package mounted into every
# container, not local to this service) — only reliably present already when
# another test file that adds it (e.g. test_rollout_ramp.py) happens to be
# collected first in the same pytest session; this file must not depend on
# collection order to import cleanly on its own.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

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


# ─────────── CloudWatch telemetry target-threading (P1, 2026-09-16) ───────────
# _fire_reverify_after_delay's reverify POST body must carry the real
# deployment_target/aws_region/ecs_service_name so an AWS ECS project's
# SECOND-and-later verdicts use real CloudWatch telemetry too, not just its
# first (which gets this context directly from worker.py's canary_loop
# config). Real bug caught by these: canary_deployment_name is already
# suffixed ("checkout-canary"), so recovering the base service name
# cloudwatch_client.py's "{service_name}-{cohort}" convention expects
# requires stripping "-canary", not using the field verbatim.

_ECS_TARGET = {
    "canary_deployment_name": "checkout-canary",
    "baseline_deployment_name": "checkout-baseline",
    "path_prefix": "/api/v1/checkout",
    "tenant_id": "tenant-1",
    "deployment_target": "aws_ecs",
    "aws_region": "us-west-2",
}


class _FakeReverifyAsyncClient:
    """Shared no-op httpx.AsyncClient stub — records the POST body without
    actually sleeping or hitting the network, mirroring the FakeAsyncClient
    pattern the graduate() tests above already use."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        _FakeReverifyAsyncClient.captured["url"] = url
        _FakeReverifyAsyncClient.captured["json"] = json

        class _Resp:
            def raise_for_status(self):
                pass

        return _Resp()


def test_fire_reverify_strips_the_canary_suffix_to_recover_the_ecs_service_name(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeReverifyAsyncClient)
    _FakeReverifyAsyncClient.captured = {}

    asyncio.run(
        rollout_scheduler._fire_reverify_after_delay(
            "run-1", "tenant-1", {}, 0, "trace-1", target=_ECS_TARGET
        )
    )

    body = _FakeReverifyAsyncClient.captured["json"]
    assert body["deployment_target"] == "aws_ecs"
    assert body["aws_region"] == "us-west-2"
    assert body["ecs_service_name"] == "checkout"


def test_fire_reverify_defaults_to_kubernetes_when_target_is_none(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeReverifyAsyncClient)
    _FakeReverifyAsyncClient.captured = {}

    asyncio.run(rollout_scheduler._fire_reverify_after_delay("run-1", "tenant-1", {}, 0, "trace-1", target=None))

    body = _FakeReverifyAsyncClient.captured["json"]
    assert body["deployment_target"] == "kubernetes"
    assert body["aws_region"] is None
    assert body["ecs_service_name"] is None


def test_advance_to_next_step_forwards_target_into_the_reverify_call(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeReverifyAsyncClient)
    _FakeReverifyAsyncClient.captured = {}

    class _FakeRedis:
        async def set(self, *a, **kw):
            pass

    state = {
        "steps": [{"trafficWeight": 10, "minDurationSeconds": 0}],
        "verification_config": {},
    }

    async def _run():
        await rollout_scheduler.advance_to_next_step(
            _FakeRedis(), "run-1", "tenant-1", state, 0, trace_id="trace-1", target=_ECS_TARGET
        )
        # advance_to_next_step fires the reverify as a background task on
        # THIS event loop — yield once so it actually runs before asserting
        # (must stay in the same asyncio.run() call: a task created via
        # create_task is bound to its loop and gets cancelled the instant
        # that loop closes, so a second, separate asyncio.run() call would
        # never observe it).
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert _FakeReverifyAsyncClient.captured["json"]["ecs_service_name"] == "checkout"


def test_schedule_retry_of_current_step_forwards_target_into_the_reverify_call(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeReverifyAsyncClient)
    _FakeReverifyAsyncClient.captured = {}

    state = {"current_step_index": 0, "verification_config": {}}

    async def _run():
        # remaining_seconds=0 deliberately — a nonzero value would make
        # _fire_reverify_after_delay actually `await asyncio.sleep(...)`
        # for real, which the single sleep(0) yield below can't wait out;
        # the real-delay path is already covered by test_rollout_ramp.py.
        await rollout_scheduler.schedule_retry_of_current_step(
            "run-1", "tenant-1", state, 0.0, trace_id="trace-1", target=_ECS_TARGET
        )
        await asyncio.sleep(0)  # see comment in the advance_to_next_step test above

    asyncio.run(_run())

    assert _FakeReverifyAsyncClient.captured["json"]["ecs_service_name"] == "checkout"
