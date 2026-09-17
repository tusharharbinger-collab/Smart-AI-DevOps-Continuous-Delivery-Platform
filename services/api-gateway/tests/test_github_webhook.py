"""
services/api-gateway/tests/test_github_webhook.py

Real gap this closes: every rollout on this platform required a human to
explicitly trigger it — a plain `git push` did nothing. Covers
webhooks_router.py's pure signature-verification/event-parsing logic and
the full receiver route (via a minimal isolated FastAPI app + fake
redis/DB, monkeypatching `_load_project`/`streams.publish` the same way
policy-controller's tests already monkeypatch module-level imported
names) against every real behavior it must have: signature rejection,
ping/non-push no-ops, delivery dedup (and that a genuine mid-processing
failure does NOT permanently swallow a legitimate retry), unmapped-repo/
wrong-branch no-ops, and the happy path queuing a real gate1 (build+test)
check — see test_gate1_result_endpoint.py and
test_gate1_check_consumer.py (pipeline-worker) for what happens after
that, and PROJECT_STATUS.md for why the webhook no longer triggers a
rollout directly (P0, 2026-09-16).
"""
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routers import webhooks_router as wh_module

WEBHOOK_SECRET = "test-webhook-secret"


class FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)


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


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _push_payload(
    full_name="octocat/hello-world", branch="main", after="a" * 40, message="fix: real bug",
) -> dict:
    return {
        "ref": f"refs/heads/{branch}",
        "after": after,
        "deleted": False,
        "repository": {"full_name": full_name},
        "head_commit": {"id": after, "message": message},
        "commits": [{"id": after, "message": message}],
    }


@pytest.fixture(autouse=True)
def _configure_secret(monkeypatch):
    monkeypatch.setattr(wh_module.settings, "GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest.fixture
def client(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(wh_module, "AsyncSessionLocal", lambda: FakeDBSession())

    app = FastAPI()
    app.state.redis = fake_redis
    app.include_router(wh_module.router)
    # raise_server_exceptions=False: an unhandled exception from the route
    # (the mid-processing-failure test deliberately raises one) must come
    # back as a real 500 response to assert on, not propagate into the test
    # itself the way TestClient's default behavior would.
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.fake_redis = fake_redis
    return test_client


def _post(client, payload: dict, event: str = "push", delivery_id: str = "delivery-1", secret: str = WEBHOOK_SECRET, sign: bool = True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery_id, "Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(secret, body)
    return client.post("/github", content=body, headers=headers)


# ─────────────────────────── pure functions ───────────────────────────


def test_verify_signature_accepts_correctly_signed_body():
    body = b'{"a": 1}'
    sig = _sign("s3cret", body)
    assert wh_module.verify_signature("s3cret", body, sig) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"a": 1}'
    sig = _sign("s3cret", body)
    assert wh_module.verify_signature("different-secret", body, sig) is False


def test_verify_signature_rejects_missing_header():
    assert wh_module.verify_signature("s3cret", b"{}", None) is False


def test_verify_signature_rejects_header_without_sha256_prefix():
    assert wh_module.verify_signature("s3cret", b"{}", "deadbeef") is False


def test_verify_signature_rejects_when_no_secret_configured():
    body = b'{"a": 1}'
    sig = _sign("s3cret", body)
    assert wh_module.verify_signature("", body, sig) is False


def test_parse_push_event_normal_push():
    result = wh_module.parse_push_event(_push_payload())
    assert result == {
        "full_name": "octocat/hello-world",
        "branch": "main",
        "commit_sha": "a" * 40,
        "commit_message": "fix: real bug",
    }


def test_parse_push_event_branch_deletion_returns_none():
    payload = _push_payload()
    payload["deleted"] = True
    assert wh_module.parse_push_event(payload) is None


def test_parse_push_event_tag_push_returns_none():
    payload = _push_payload()
    payload["ref"] = "refs/tags/v1.0.0"
    assert wh_module.parse_push_event(payload) is None


def test_parse_push_event_zero_sha_returns_none():
    payload = _push_payload(after="0" * 40)
    assert wh_module.parse_push_event(payload) is None


def test_parse_push_event_falls_back_to_last_commit_message_when_no_head_commit():
    payload = _push_payload()
    payload["head_commit"] = None
    payload["commits"] = [{"id": "x", "message": "first"}, {"id": "a" * 40, "message": "second"}]
    result = wh_module.parse_push_event(payload)
    assert result["commit_message"] == "second"


def test_parse_push_event_missing_repository_returns_none():
    payload = _push_payload()
    payload["repository"] = {}
    assert wh_module.parse_push_event(payload) is None


# ─────────────────────────── route behavior ───────────────────────────


def test_missing_secret_configured_returns_503(client, monkeypatch):
    monkeypatch.setattr(wh_module.settings, "GITHUB_WEBHOOK_SECRET", "")
    resp = _post(client, _push_payload(), sign=False)
    assert resp.status_code == 503


def test_invalid_signature_returns_401(client):
    resp = _post(client, _push_payload(), secret="wrong-secret")
    assert resp.status_code == 401


def test_ping_event_acked_without_processing(client, monkeypatch):
    calls = []

    async def fake_publish(redis_client, stream, payload):
        calls.append(payload)
        return "1-0"

    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)
    resp = _post(client, {"zen": "Design for failure."}, event="ping")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pong"
    assert calls == []


def test_non_push_event_is_ignored(client, monkeypatch):
    calls = []

    async def fake_publish(redis_client, stream, payload):
        calls.append(payload)
        return "1-0"

    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)
    resp = _post(client, {}, event="issues")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert calls == []


def test_missing_delivery_id_returns_400(client):
    body = json.dumps(_push_payload()).encode("utf-8")
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _sign(WEBHOOK_SECRET, body),
    }
    resp = client.post("/github", content=body, headers=headers)
    assert resp.status_code == 400


def test_duplicate_delivery_is_a_no_op(client, monkeypatch):
    calls = []

    async def fake_publish(redis_client, stream, payload):
        calls.append((stream, payload))
        return "1-0"

    async def fake_load_project(db, project_id, tenant_id):
        return {"pipeline_id": "pipe-1", "branch": "main"}

    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)
    monkeypatch.setattr(wh_module, "_load_project", fake_load_project)
    import asyncio

    asyncio.run(wh_module.webhook_registry.register(client.fake_redis, "octocat/hello-world", "proj-1", "tenant-1", "main"))

    first = _post(client, _push_payload(), delivery_id="delivery-dup")
    second = _post(client, _push_payload(), delivery_id="delivery-dup")

    assert first.status_code == 200
    assert first.json()["status"] == "gate1_check_queued"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_ignored"
    assert len(calls) == 1, "a replayed delivery must not queue a second gate1 check"


def test_unmapped_repo_is_ignored_and_does_not_queue_a_check(client, monkeypatch):
    calls = []

    async def fake_publish(redis_client, stream, payload):
        calls.append(payload)
        return "1-0"

    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)

    resp = _post(client, _push_payload(full_name="someone/unconnected-repo"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert calls == []


def test_push_to_untracked_branch_is_ignored(client, monkeypatch):
    calls = []

    async def fake_publish(redis_client, stream, payload):
        calls.append(payload)
        return "1-0"

    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)
    import asyncio

    asyncio.run(wh_module.webhook_registry.register(client.fake_redis, "octocat/hello-world", "proj-1", "tenant-1", "main"))

    resp = _post(client, _push_payload(branch="feature/not-tracked"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert "feature/not-tracked" in resp.json()["reason"]
    assert calls == []


def test_happy_path_push_to_tracked_branch_queues_a_real_gate1_check(client, monkeypatch):
    captured = {}

    async def fake_publish(redis_client, stream, payload):
        captured["stream"] = stream
        captured["payload"] = payload
        return "1-0"

    async def fake_load_project(db, project_id, tenant_id):
        assert project_id == "proj-1"
        assert tenant_id == "tenant-1"
        return {
            "pipeline_id": "pipe-1", "branch": "main", "repo_url": "https://github.com/octocat/hello-world.git",
            "root_directory": None, "dockerfile_path": "Dockerfile", "language": None,
            "manifest_path": None, "start_command": None, "test_command": "pytest",
        }

    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)
    monkeypatch.setattr(wh_module, "_load_project", fake_load_project)
    import asyncio

    asyncio.run(wh_module.webhook_registry.register(client.fake_redis, "octocat/hello-world", "proj-1", "tenant-1", "main"))

    resp = _post(client, _push_payload(message="fix: the real bug"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "gate1_check_queued"
    assert body["project_id"] == "proj-1"
    assert "gate1_check_id" in body

    assert captured["stream"] == wh_module.STREAM_GATE1_CHECK
    queued = captured["payload"]
    assert queued["project_id"] == "proj-1"
    assert queued["tenant_id"] == "tenant-1"
    assert queued["repo_url"] == "https://github.com/octocat/hello-world.git"
    assert queued["ref"] == "main"
    assert queued["dockerfile_path"] == "Dockerfile"
    assert queued["test_command"] == "pytest"
    assert queued["commit_sha"] == "a" * 40
    assert queued["commit_message"] == "fix: the real bug"
    assert queued["gate1_check_id"] == body["gate1_check_id"]


def test_project_no_longer_existing_is_ignored_not_500(client, monkeypatch):
    from fastapi import HTTPException

    async def fake_load_project(db, project_id, tenant_id):
        raise HTTPException(status_code=404, detail="Project not found")

    monkeypatch.setattr(wh_module, "_load_project", fake_load_project)
    import asyncio

    asyncio.run(wh_module.webhook_registry.register(client.fake_redis, "octocat/hello-world", "proj-1", "tenant-1", "main"))

    resp = _post(client, _push_payload())

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_mid_processing_failure_releases_dedup_key_for_a_legitimate_retry(client, monkeypatch):
    """A genuine failure (not a real duplicate) must not permanently block a
    later retry of the SAME delivery id — this is what stands in for a real
    polling/redelivery safety net until one exists (see BACKLOG.md)."""

    async def failing_load_project(db, project_id, tenant_id):
        raise RuntimeError("database temporarily unreachable")

    monkeypatch.setattr(wh_module, "_load_project", failing_load_project)
    import asyncio

    asyncio.run(wh_module.webhook_registry.register(client.fake_redis, "octocat/hello-world", "proj-1", "tenant-1", "main"))

    first = _post(client, _push_payload(), delivery_id="delivery-retry")
    assert first.status_code == 500

    # Second attempt (GitHub's own retry, or a human clicking "Redeliver")
    # with the SAME delivery id must actually be reprocessed, not silently
    # swallowed as a duplicate — this time it succeeds.
    async def succeeding_load_project(db, project_id, tenant_id):
        return {
            "pipeline_id": "pipe-1", "branch": "main", "repo_url": "https://github.com/octocat/hello-world.git",
            "root_directory": None, "dockerfile_path": "Dockerfile", "language": None,
            "manifest_path": None, "start_command": None, "test_command": "pytest",
        }

    async def fake_publish(redis_client, stream, payload):
        return "1-0"

    monkeypatch.setattr(wh_module, "_load_project", succeeding_load_project)
    monkeypatch.setattr(wh_module.streams, "publish", fake_publish)

    second = _post(client, _push_payload(), delivery_id="delivery-retry")
    assert second.status_code == 200
    assert second.json()["status"] == "gate1_check_queued"
