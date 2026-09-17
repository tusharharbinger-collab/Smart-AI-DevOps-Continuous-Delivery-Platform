"""
services/pipeline-worker/tests/test_gate1_check_consumer.py

Gate 1 (P0, 2026-09-16) — real gap this closes: build_preview.py's real
build+test dry run existed and was live-tested for the onboarding wizard,
but nothing autonomous ever called it. Covers
_process_gate1_check_message (main.py): runs the real dry run (mocked
here) and calls back into api-gateway's
POST /internal/{project_id}/gate1-result with the real pass/fail —
verifying both outcomes reach the callback with the right shape, the
repo_private/credentials decision is never made unsafely (see
CLAUDE.md's documented trap this deliberately avoids reintroducing), and
that a passed dry run and a failed one are never confused for each other.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json

import httpx
import pytest

import src.main as main_module


class FakeRedisSync:
    """run_build_preview takes a SYNC redis client — never awaited."""

    def rpush(self, *a, **kw):
        pass

    def expire(self, *a, **kw):
        pass

    def set(self, *a, **kw):
        pass

    def get(self, *a, **kw):
        return None


class FakeApp:
    def __init__(self):
        self.state = type("S", (), {"redis_sync": FakeRedisSync()})()


_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    return lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"})


def _payload(**overrides):
    base = {
        "gate1_check_id": "gate1-run-1",
        "project_id": "proj-1",
        "tenant_id": "tenant-1",
        "repo_url": "https://github.com/octocat/hello-world.git",
        "ref": "main",
        "root_directory": None,
        "dockerfile_path": "Dockerfile",
        "language": None,
        "manifest_path": None,
        "start_command": None,
        "test_command": "pytest",
        "commit_sha": "a" * 40,
        "commit_message": "fix: real bug",
    }
    base.update(overrides)
    return base


def test_passing_check_calls_back_with_passed_true(monkeypatch):
    monkeypatch.setattr(main_module, "run_build_preview", lambda *a, **kw: {"status": "succeeded", "dockerfile_path": "Dockerfile"})

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "TRIGGERED"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    asyncio.run(main_module._process_gate1_check_message(FakeApp(), _payload()))

    assert captured["url"].endswith("/api/v1/projects/internal/proj-1/gate1-result")
    assert captured["body"]["tenant_id"] == "tenant-1"
    assert captured["body"]["passed"] is True
    assert captured["body"]["commit_sha"] == "a" * 40
    assert captured["body"]["commit_message"] == "fix: real bug"
    assert "stage" not in captured["body"]


def test_failing_check_calls_back_with_failure_detail(monkeypatch):
    monkeypatch.setattr(
        main_module, "run_build_preview",
        lambda *a, **kw: {"status": "failed", "stage": "build", "error": "COPY failed: file not found", "human_side": True},
    )

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "BLOCKED"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    asyncio.run(main_module._process_gate1_check_message(FakeApp(), _payload()))

    assert captured["body"]["passed"] is False
    assert captured["body"]["stage"] == "build"
    assert captured["body"]["error"] == "COPY failed: file not found"
    assert captured["body"]["human_side"] is True


def test_raises_when_the_api_gateway_callback_itself_fails(monkeypatch):
    """The consumer loop (not tested here) relies on this propagating so the
    stream entry stays unacked and gets retried — a callback failure must
    never be silently swallowed."""
    monkeypatch.setattr(main_module, "run_build_preview", lambda *a, **kw: {"status": "succeeded", "dockerfile_path": "Dockerfile"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="api-gateway unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(main_module._process_gate1_check_message(FakeApp(), _payload()))


def test_repo_private_flag_is_only_set_when_github_token_is_genuinely_configured(monkeypatch):
    """Real trap this must never reintroduce (see CLAUDE.md): unconditionally
    naming credentialsEnvVar when the actual env var is unset hard-fails
    EVERY public-repo clone. repo_private must only be True when a real,
    non-empty GITHUB_TOKEN exists in this container's own environment."""
    seen_config = {}

    def fake_run_build_preview(redis_sync, run_id, config):
        seen_config.update(config)
        return {"status": "succeeded", "dockerfile_path": "Dockerfile"}

    monkeypatch.setattr(main_module, "run_build_preview", fake_run_build_preview)
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(lambda req: httpx.Response(200, json={})))

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    asyncio.run(main_module._process_gate1_check_message(FakeApp(), _payload(gate1_check_id="no-token-run")))
    assert seen_config["repo_private"] is False

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_realtoken")
    asyncio.run(main_module._process_gate1_check_message(FakeApp(), _payload(gate1_check_id="with-token-run")))
    assert seen_config["repo_private"] is True


def test_config_passed_to_build_preview_carries_every_field_from_the_payload(monkeypatch):
    seen_config = {}

    def fake_run_build_preview(redis_sync, run_id, config):
        seen_config.update(config)
        return {"status": "succeeded", "dockerfile_path": "Dockerfile"}

    monkeypatch.setattr(main_module, "run_build_preview", fake_run_build_preview)
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(lambda req: httpx.Response(200, json={})))

    asyncio.run(
        main_module._process_gate1_check_message(
            FakeApp(),
            _payload(
                root_directory="services/api", dockerfile_path=None, language="python",
                manifest_path="requirements.txt", start_command="python main.py", ref="develop",
            ),
        )
    )

    assert seen_config["repo_url"] == "https://github.com/octocat/hello-world.git"
    assert seen_config["ref"] == "develop"
    assert seen_config["root_directory"] == "services/api"
    assert seen_config["language"] == "python"
    assert seen_config["manifest_path"] == "requirements.txt"
    assert seen_config["start_command"] == "python main.py"
    assert seen_config["test_command"] == "pytest"
