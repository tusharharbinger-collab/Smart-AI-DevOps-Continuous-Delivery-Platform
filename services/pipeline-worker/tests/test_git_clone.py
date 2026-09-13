"""
services/pipeline-worker/tests/test_git_clone.py

Phase 2 hardening: a build stage that declares `repoUrl` clones the actual
onboarded service's own repo into an isolated per-run workspace instead of
building from a fixed local path — closes the real gap where "build" always
meant "rebuild the platform's own demo app," never an arbitrary user repo.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.tasks import git_clone


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def test_authenticated_url_embeds_token_from_env_var(monkeypatch):
    monkeypatch.setenv("MY_GIT_TOKEN", "test-token-value")
    result = git_clone._authenticated_url("https://github.com/acme/service.git", "MY_GIT_TOKEN")
    assert result == "https://x-access-token:test-token-value@github.com/acme/service.git"


def test_authenticated_url_passthrough_when_no_credentials_env_var():
    result = git_clone._authenticated_url("https://github.com/acme/service.git", None)
    assert result == "https://github.com/acme/service.git"


def test_authenticated_url_raises_when_env_var_unset():
    with pytest.raises(git_clone.GitCloneError, match="empty/unset"):
        git_clone._authenticated_url("https://github.com/acme/service.git", "NEVER_SET_THIS_VAR")


def test_authenticated_url_rejects_non_https_with_credentials(monkeypatch):
    monkeypatch.setenv("MY_GIT_TOKEN", "test-token-value")
    with pytest.raises(git_clone.GitCloneError, match="https://"):
        git_clone._authenticated_url("git@github.com:acme/service.git", "MY_GIT_TOKEN")


def test_clone_repo_for_run_success(monkeypatch, tmp_path):
    monkeypatch.setattr(git_clone, "WORKSPACE_ROOT", str(tmp_path))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        os.makedirs(cmd[-1], exist_ok=True)  # simulate git actually creating the target dir
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(git_clone.subprocess, "run", fake_run)

    workspace = git_clone.clone_repo_for_run("run-1", "https://github.com/acme/service.git", ref="main")
    assert workspace == str(tmp_path / "run-1")
    assert os.path.isdir(workspace)
    assert captured["cmd"][:2] == ["git", "clone"]
    assert "https://github.com/acme/service.git" in captured["cmd"]


def test_clone_repo_for_run_never_leaks_token_into_the_clone_command_log(monkeypatch, tmp_path):
    """The credential-bearing URL must reach the actual git command, but never a raised error message."""
    monkeypatch.setattr(git_clone, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("MY_GIT_TOKEN", "super-secret-token")

    def fake_run(cmd, **kwargs):
        assert "super-secret-token" in cmd[-2]  # git DOES get the real authenticated URL
        return _FakeCompletedProcess(returncode=1, stderr="fatal: repository not found")

    monkeypatch.setattr(git_clone.subprocess, "run", fake_run)

    with pytest.raises(git_clone.GitCloneError) as exc_info:
        git_clone.clone_repo_for_run(
            "run-2", "https://github.com/acme/private-repo.git", credentials_env_var="MY_GIT_TOKEN"
        )
    assert "super-secret-token" not in str(exc_info.value)
    assert "https://github.com/acme/private-repo.git" in str(exc_info.value)


def test_clone_repo_for_run_cleans_up_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(git_clone, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        git_clone.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(returncode=128, stderr="fatal: error")
    )
    with pytest.raises(git_clone.GitCloneError):
        git_clone.clone_repo_for_run("run-3", "https://github.com/acme/service.git")
    assert not os.path.exists(tmp_path / "run-3")


def test_clone_repo_for_run_raises_on_timeout(monkeypatch, tmp_path):
    import subprocess as real_subprocess

    monkeypatch.setattr(git_clone, "WORKSPACE_ROOT", str(tmp_path))

    def _raise_timeout(*a, **kw):
        raise real_subprocess.TimeoutExpired(cmd="git clone", timeout=120)

    monkeypatch.setattr(git_clone.subprocess, "run", _raise_timeout)
    with pytest.raises(git_clone.GitCloneError, match="timed out"):
        git_clone.clone_repo_for_run("run-4", "https://github.com/acme/service.git")


def test_cleanup_workspace_removes_the_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(git_clone, "WORKSPACE_ROOT", str(tmp_path))
    workspace = tmp_path / "run-5"
    workspace.mkdir()
    (workspace / "some_file.txt").write_text("data")

    git_clone.cleanup_workspace("run-5")
    assert not workspace.exists()


def test_cleanup_workspace_is_a_no_op_when_nothing_to_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(git_clone, "WORKSPACE_ROOT", str(tmp_path))
    git_clone.cleanup_workspace("run-never-existed")  # must not raise
