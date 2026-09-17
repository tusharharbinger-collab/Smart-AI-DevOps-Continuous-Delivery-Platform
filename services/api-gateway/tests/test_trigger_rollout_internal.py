"""
services/api-gateway/tests/test_trigger_rollout_internal.py

Covers _trigger_rollout_internal (projects_router.py) — the core trigger
logic extracted so POST /{project_id}/rollout (human-triggered) and the
GitHub webhook receiver (webhooks_router.py) share byte-identical
actuation behavior. This is the one function both paths now depend on, so
a regression here would silently break either trigger path.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import pytest
from fastapi import HTTPException

from src.routers import projects_router


class FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeDB:
    def __init__(self, pipeline_row=None):
        self.executed = []
        self.committed = False
        self._pipeline_row = pipeline_row

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if "SELECT policy_yaml FROM pipelines" in sql:
            return FakeResult(self._pipeline_row)
        return FakeResult(None)

    async def commit(self):
        self.committed = True


class FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value


def _project(pipeline_id="pipe-1", canary_tag=None):
    return {"pipeline_id": pipeline_id, "canary_tag": canary_tag}


def test_raises_409_when_project_has_no_linked_pipeline():
    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router._trigger_rollout_internal(
                db=db, redis_client=FakeRedis(), tenant_id="tenant-1",
                project={"pipeline_id": None}, project_id="proj-1", trigger_type="MANUAL_UI",
            )
        )
    assert exc_info.value.status_code == 409


def test_raises_404_when_linked_pipeline_row_is_missing():
    db = FakeDB(pipeline_row=None)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router._trigger_rollout_internal(
                db=db, redis_client=FakeRedis(), tenant_id="tenant-1",
                project=_project(), project_id="proj-1", trigger_type="MANUAL_UI",
            )
        )
    assert exc_info.value.status_code == 404


def test_happy_path_inserts_execution_commits_and_publishes(monkeypatch):
    published = {}

    async def fake_publish(redis_client, stream, payload):
        published["stream"] = stream
        published["payload"] = payload
        return "1-0"

    monkeypatch.setattr(projects_router.streams, "publish", fake_publish)

    db = FakeDB(pipeline_row={"policy_yaml": "spec: {}"})
    redis_client = FakeRedis()

    run_id = asyncio.run(
        projects_router._trigger_rollout_internal(
            db=db, redis_client=redis_client, tenant_id="tenant-1",
            project=_project(canary_tag="v1.2.0"), project_id="proj-1",
            trigger_type="GITHUB_PUSH", commit_sha="deadbeef", commit_message="fix: bug",
        )
    )

    assert run_id  # a real uuid string
    assert db.committed is True
    insert_calls = [p for sql, p in db.executed if "INSERT INTO pipeline_executions" in sql]
    assert len(insert_calls) == 1
    assert insert_calls[0]["trigger_type"] == "GITHUB_PUSH"
    assert insert_calls[0]["commit_sha"] == "deadbeef"
    assert insert_calls[0]["commit_message"] == "fix: bug"
    assert insert_calls[0]["target_version"] == "v1.2.0"

    assert published["stream"] == projects_router.STREAM_PIPELINE_START
    assert published["payload"]["pipeline_run_id"] == run_id
    assert published["payload"]["target_version"] == "v1.2.0"


def test_target_version_falls_back_to_default_when_no_canary_tag(monkeypatch):
    async def fake_publish(*a, **kw):
        return "1-0"

    monkeypatch.setattr(projects_router.streams, "publish", fake_publish)
    db = FakeDB(pipeline_row={"policy_yaml": "spec: {}"})

    asyncio.run(
        projects_router._trigger_rollout_internal(
            db=db, redis_client=FakeRedis(), tenant_id="tenant-1",
            project=_project(canary_tag=None), project_id="proj-1", trigger_type="MANUAL_UI",
        )
    )

    insert_calls = [p for sql, p in db.executed if "INSERT INTO pipeline_executions" in sql]
    assert insert_calls[0]["target_version"] == "v1.1.0"


def test_no_user_id_skips_github_credential_broker(monkeypatch):
    async def fake_publish(*a, **kw):
        return "1-0"

    monkeypatch.setattr(projects_router.streams, "publish", fake_publish)
    db = FakeDB(pipeline_row={"policy_yaml": "spec: {}"})
    redis_client = FakeRedis()

    run_id = asyncio.run(
        projects_router._trigger_rollout_internal(
            db=db, redis_client=redis_client, tenant_id="tenant-1",
            project=_project(), project_id="proj-1", trigger_type="GITHUB_PUSH", user_id=None,
        )
    )

    assert f"clone_token:{run_id}" not in redis_client._store


def test_user_id_with_stored_github_token_broker_runs(monkeypatch):
    async def fake_publish(*a, **kw):
        return "1-0"

    monkeypatch.setattr(projects_router.streams, "publish", fake_publish)
    db = FakeDB(pipeline_row={"policy_yaml": "spec: {}"})
    redis_client = FakeRedis()
    redis_client._store["github:token:user-1"] = "gho_faketoken"

    run_id = asyncio.run(
        projects_router._trigger_rollout_internal(
            db=db, redis_client=redis_client, tenant_id="tenant-1",
            project=_project(), project_id="proj-1", trigger_type="MANUAL_UI", user_id="user-1",
        )
    )

    assert redis_client._store[f"clone_token:{run_id}"] == "gho_faketoken"
