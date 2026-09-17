"""
services/api-gateway/tests/test_webhook_polling_fallback.py

Covers poll_for_missed_webhook_deliveries / _poll_one_repo (webhooks_router.py)
— the safety net for a webhook delivery GitHub itself never retried, or
this platform being unreachable when it tried. Verifies: a real mismatch
between GitHub's branch HEAD and the last commit actually checked queues
the exact same gate1 check the real-time path would; an already-checked
commit is correctly a no-op; a GitHub API failure for one repo never
kills the whole poll cycle.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json

import pytest
from fastapi import HTTPException

from src.routers import webhooks_router as wh_module


class FakeRedis:
    def __init__(self, store: dict[str, str] | None = None):
        self._store: dict[str, str] = store or {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)

    async def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                yield key


class _FakeBeginCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeDBSession:
    async def execute(self, *args, **kwargs):
        return None

    def begin(self):
        return _FakeBeginCtx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _mapping(project_id="proj-1", tenant_id="tenant-1", branch="main"):
    return {"project_id": project_id, "tenant_id": tenant_id, "branch": branch}


def _project():
    return {
        "pipeline_id": "pipe-1", "repo_url": "https://github.com/octocat/hello-world.git",
        "root_directory": None, "dockerfile_path": "Dockerfile", "language": None,
        "manifest_path": None, "start_command": None, "test_command": "pytest",
    }


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    monkeypatch.setattr(wh_module, "AsyncSessionLocal", lambda: FakeDBSession())


def test_queues_a_check_when_real_head_differs_from_last_checked(monkeypatch):
    captured = {}

    async def fake_github_get(path, token, params=None):
        assert path == "/repos/octocat/hello-world/commits/main"
        return {"sha": "new" + "a" * 37, "commit": {"message": "fix: real bug"}}

    async def fake_load_project(db, project_id, tenant_id):
        return _project()

    async def fake_queue(redis_client, **kwargs):
        captured.update(kwargs)
        return "gate1-check-id"

    monkeypatch.setattr(wh_module, "_github_get", fake_github_get)
    monkeypatch.setattr(wh_module, "_load_project", fake_load_project)
    monkeypatch.setattr(wh_module, "_queue_gate1_check", fake_queue)

    redis_client = FakeRedis({"gate1_last_result:proj-1": json.dumps({"passed": True, "commit_sha": "old" + "b" * 37})})

    asyncio.run(wh_module._poll_one_repo(redis_client, "octocat/hello-world", _mapping()))

    assert captured["commit_sha"] == "new" + "a" * 37
    assert captured["commit_message"] == "fix: real bug"
    assert captured["project_id"] == "proj-1"
    assert captured["tenant_id"] == "tenant-1"
    assert captured["repo_url"] == "https://github.com/octocat/hello-world.git"


def test_no_op_when_real_head_matches_last_checked_commit(monkeypatch):
    same_sha = "same" + "c" * 36

    async def fake_github_get(path, token, params=None):
        return {"sha": same_sha, "commit": {"message": "already checked"}}

    async def exploding_queue(redis_client, **kwargs):
        raise AssertionError("must not queue a check for a commit already checked")

    monkeypatch.setattr(wh_module, "_github_get", fake_github_get)
    monkeypatch.setattr(wh_module, "_queue_gate1_check", exploding_queue)

    redis_client = FakeRedis({"gate1_last_result:proj-1": json.dumps({"passed": True, "commit_sha": same_sha})})

    asyncio.run(wh_module._poll_one_repo(redis_client, "octocat/hello-world", _mapping()))
    # No assertion error raised means the exploding queue was correctly never called.


def test_no_op_when_no_prior_gate1_result_and_project_missing(monkeypatch):
    """A project with no gate1_last_result yet (never pushed to since connecting)
    still safely proceeds to compare against None — first real check is expected
    to fire, but if the project itself is gone, it's ignored, not an error."""

    async def fake_github_get(path, token, params=None):
        return {"sha": "a" * 40, "commit": {"message": "first ever push"}}

    async def fake_load_project_missing(db, project_id, tenant_id):
        raise HTTPException(status_code=404, detail="Project not found")

    called = []

    async def exploding_queue(redis_client, **kwargs):
        called.append(kwargs)

    monkeypatch.setattr(wh_module, "_github_get", fake_github_get)
    monkeypatch.setattr(wh_module, "_load_project", fake_load_project_missing)
    monkeypatch.setattr(wh_module, "_queue_gate1_check", exploding_queue)

    redis_client = FakeRedis()

    asyncio.run(wh_module._poll_one_repo(redis_client, "octocat/hello-world", _mapping()))

    assert called == []


def test_github_api_failure_for_one_repo_does_not_raise(monkeypatch):
    async def failing_github_get(path, token, params=None):
        raise HTTPException(status_code=502, detail="Could not reach GitHub")

    monkeypatch.setattr(wh_module, "_github_get", failing_github_get)

    redis_client = FakeRedis()
    # Must not raise.
    asyncio.run(wh_module._poll_one_repo(redis_client, "octocat/hello-world", _mapping()))


def test_full_poll_cycle_checks_every_mapped_repo_and_survives_one_failure(monkeypatch):
    calls = []

    async def fake_github_get(path, token, params=None):
        if "broken-repo" in path:
            raise HTTPException(status_code=502, detail="unreachable")
        calls.append(path)
        return {"sha": "a" * 40, "commit": {"message": "m"}}

    async def fake_load_project(db, project_id, tenant_id):
        return _project()

    queued = []

    async def fake_queue(redis_client, **kwargs):
        queued.append(kwargs["project_id"])
        return "id"

    monkeypatch.setattr(wh_module, "_github_get", fake_github_get)
    monkeypatch.setattr(wh_module, "_load_project", fake_load_project)
    monkeypatch.setattr(wh_module, "_queue_gate1_check", fake_queue)

    redis_client = FakeRedis(
        {
            "webhook:repo:octocat/good-repo": json.dumps(_mapping(project_id="proj-good")),
            "webhook:repo:octocat/broken-repo": json.dumps(_mapping(project_id="proj-broken")),
        }
    )

    asyncio.run(wh_module.poll_for_missed_webhook_deliveries(redis_client))

    assert "proj-good" in queued
    assert "proj-broken" not in queued
    assert len(calls) == 1
