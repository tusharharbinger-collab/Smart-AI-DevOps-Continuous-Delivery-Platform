"""
services/pipeline-worker/tests/test_build_preview.py

Covers run_build_preview's orchestration: Dockerfile-present path,
synthesized-Dockerfile path, the genuinely-unsupported path, and the
human_side vs platform_side failure classification the "100% surety we
build unless it's a human error" guarantee rests on.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.build_preview as build_preview_module
from src.build_preview import run_build_preview


class _FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def expire(self, key, seconds):
        pass

    def set(self, key, value, ex=None):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)


def _result(redis, run_id):
    return json.loads(redis.strings[f"preview_result:{run_id}"])


def test_dockerfile_present_path_builds_and_tests_successfully(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(
        build_preview_module,
        "run_build_task",
        lambda *a, **kw: {"status": "success", "image": "x", "workspace": "/tmp/ws"},
    )
    monkeypatch.setattr(build_preview_module, "run_test_task", lambda *a, **kw: {"status": "success"})
    monkeypatch.setattr(build_preview_module, "cleanup_workspace", lambda run_id: None)

    returned = run_build_preview(
        redis,
        "run-1",
        {"repo_url": "https://github.com/x/y.git", "ref": "main", "dockerfile_path": "Dockerfile", "test_command": "pytest"},
    )

    result = _result(redis, "run-1")
    assert result["status"] == "succeeded"
    logs = redis.lists["preview_logs:run-1"]
    assert any("Using the repo's own Dockerfile" in line for line in logs)
    assert any("Tests passed" in line for line in logs)
    # Gate 1 (P0, 2026-09-16) reads this return value directly instead of a
    # second Redis round-trip — must mirror the Redis-side result exactly.
    assert returned == {"status": "succeeded", "dockerfile_path": "Dockerfile", "test_warning": None}


def test_return_value_mirrors_the_redis_result_on_a_build_failure(monkeypatch):
    redis = _FakeRedis()

    def _raise_build_error(*a, **kw):
        raise RuntimeError("Build failed: file not found")

    monkeypatch.setattr(build_preview_module, "run_build_task", _raise_build_error)

    returned = run_build_preview(
        redis, "run-return-fail", {"repo_url": "https://github.com/x/y.git", "ref": "main", "dockerfile_path": "Dockerfile"}
    )

    assert returned["status"] == "failed"
    assert returned["stage"] == "build"
    assert returned["human_side"] is True
    # The Redis-stored result additionally carries `updated_at` — the
    # return value and the Redis record must still agree on everything else.
    stored = _result(redis, "run-return-fail")
    assert {k: v for k, v in stored.items() if k != "updated_at"} == returned


def test_synthesized_dockerfile_path_when_no_dockerfile_present(monkeypatch):
    redis = _FakeRedis()
    captured = {}

    def _fake_run_build_task(run_id, dockerfile_path, tag, **kw):
        captured["dockerfile_path"] = dockerfile_path
        captured["dockerfile_content"] = kw.get("dockerfile_content")
        return {"status": "success", "image": "x", "workspace": None}

    monkeypatch.setattr(build_preview_module, "run_build_task", _fake_run_build_task)
    monkeypatch.setattr(build_preview_module, "cleanup_workspace", lambda run_id: None)

    run_build_preview(
        redis,
        "run-2",
        {
            "repo_url": "https://github.com/x/y.git",
            "ref": "main",
            "language": "python",
            "manifest_path": "requirements.txt",
            "start_command": "python app.py",
        },
    )

    result = _result(redis, "run-2")
    assert result["status"] == "succeeded"
    assert captured["dockerfile_path"] == "Dockerfile"
    assert "FROM python" in captured["dockerfile_content"]
    logs = redis.lists["preview_logs:run-2"]
    assert any("synthesized one for python" in line for line in logs)
    assert any("skipping test verification" in line for line in logs)  # no test_command given


def test_synthesizes_even_when_manifest_path_is_absent(monkeypatch):
    """Real gap found live: a smartcd.yaml declaring `runtime`+`startCommand`
    never sets manifest_path (parse_yaml_manifest has no such field) — this
    used to require manifest_path truthy before attempting synthesis at all,
    so a manifest-declared project failed the onboarding PREVIEW with "no
    build method found" even though worker.py's real pipeline build stage
    (which never required manifest_path either) built the exact same config
    successfully. Preview and real-rollout answers must never diverge."""
    redis = _FakeRedis()
    captured = {}

    def _fake_run_build_task(run_id, dockerfile_path, tag, **kw):
        captured["dockerfile_path"] = dockerfile_path
        captured["dockerfile_content"] = kw.get("dockerfile_content")
        return {"status": "success", "image": "x", "workspace": None}

    monkeypatch.setattr(build_preview_module, "run_build_task", _fake_run_build_task)
    monkeypatch.setattr(build_preview_module, "cleanup_workspace", lambda run_id: None)

    run_build_preview(
        redis,
        "run-manifestless",
        {
            "repo_url": "https://github.com/x/y.git",
            "ref": "main",
            "language": "python",
            "manifest_path": None,
            "start_command": "python main.py",
        },
    )

    result = _result(redis, "run-manifestless")
    assert result["status"] == "succeeded"
    assert captured["dockerfile_path"] == "Dockerfile"
    assert "requirements.txt" in captured["dockerfile_content"]


def test_genuinely_unsupported_repo_fails_as_human_side_with_no_build_attempt(monkeypatch):
    redis = _FakeRedis()
    build_was_called = {"value": False}
    monkeypatch.setattr(
        build_preview_module, "run_build_task", lambda *a, **kw: build_was_called.__setitem__("value", True)
    )

    run_build_preview(redis, "run-3", {"repo_url": "https://github.com/x/y.git", "ref": "main"})

    result = _result(redis, "run-3")
    assert result["status"] == "failed"
    assert result["stage"] == "build"
    assert result["human_side"] is True
    assert build_was_called["value"] is False


def test_real_build_failure_is_classified_human_side(monkeypatch):
    redis = _FakeRedis()

    def _raise_build_error(*a, **kw):
        raise RuntimeError("Build failed: {'message': \"COPY failed: file not found\"}")

    monkeypatch.setattr(build_preview_module, "run_build_task", _raise_build_error)

    run_build_preview(
        redis, "run-4", {"repo_url": "https://github.com/x/y.git", "ref": "main", "dockerfile_path": "Dockerfile"}
    )

    result = _result(redis, "run-4")
    assert result["status"] == "failed"
    assert result["human_side"] is True


def test_docker_daemon_unreachable_is_classified_platform_side_not_human(monkeypatch):
    redis = _FakeRedis()

    def _raise_daemon_error(*a, **kw):
        raise RuntimeError("Could not reach the local Docker daemon to build x:preview: connection refused")

    monkeypatch.setattr(build_preview_module, "run_build_task", _raise_daemon_error)

    run_build_preview(
        redis, "run-5", {"repo_url": "https://github.com/x/y.git", "ref": "main", "dockerfile_path": "Dockerfile"}
    )

    result = _result(redis, "run-5")
    assert result["status"] == "failed"
    assert result["human_side"] is False


def test_test_stage_failure_does_not_block_a_successful_build(monkeypatch):
    """Real gap found live: a test command failing here used to fail the
    WHOLE preview (and, before worker.py's own fix, a real pipeline's
    deploy too) — even though a wrong/unsupported test command (e.g. a
    Python command against a JS repo, or one pipeline-worker's own
    container has no runtime for) says nothing about whether the actual
    Docker build — which may already run the project's real tests as a
    build layer — is sound. A test failure is now a visible warning, never
    a hard failure, on top of an already-successful build."""
    redis = _FakeRedis()
    monkeypatch.setattr(
        build_preview_module,
        "run_build_task",
        lambda *a, **kw: {"status": "success", "image": "x", "workspace": "/tmp/ws"},
    )

    def _raise_test_error(*a, **kw):
        raise RuntimeError("Tests failed: 1 failed, 3 passed")

    monkeypatch.setattr(build_preview_module, "run_test_task", _raise_test_error)
    monkeypatch.setattr(build_preview_module, "cleanup_workspace", lambda run_id: None)

    returned = run_build_preview(
        redis,
        "run-6",
        {
            "repo_url": "https://github.com/x/y.git",
            "ref": "main",
            "dockerfile_path": "Dockerfile",
            "test_command": "pytest",
        },
    )

    result = _result(redis, "run-6")
    assert result["status"] == "succeeded"
    assert result["test_warning"] == "Tests failed: 1 failed, 3 passed"
    assert returned["status"] == "succeeded"
    assert returned["test_warning"] == "Tests failed: 1 failed, 3 passed"
    logs = redis.lists["preview_logs:run-6"]
    assert any("non-blocking" in line for line in logs)


def test_no_test_command_configured_skips_cleanly_with_no_warning(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(
        build_preview_module,
        "run_build_task",
        lambda *a, **kw: {"status": "success", "image": "x", "workspace": "/tmp/ws"},
    )
    monkeypatch.setattr(build_preview_module, "cleanup_workspace", lambda run_id: None)

    returned = run_build_preview(
        redis,
        "run-7",
        {"repo_url": "https://github.com/x/y.git", "ref": "main", "dockerfile_path": "Dockerfile"},
    )

    assert returned["status"] == "succeeded"
    assert returned["test_warning"] is None
    logs = redis.lists["preview_logs:run-7"]
    assert any("skipping test verification" in line for line in logs)
