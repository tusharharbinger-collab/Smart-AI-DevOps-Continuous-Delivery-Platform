"""
services/pipeline-worker/tests/test_registry_preflight.py

Phase 1 hardening: verifies a canary deploy stage's image tag exists BEFORE
applying the Deployment, failing the stage immediately instead of letting
Kubernetes silently sit in ImagePullBackOff. This dev/demo stack has no real
OCI registry (see preflight.py's module docstring — images are built
locally and injected into Kind via `kind load docker-image`, nothing
listens on `localhost:5001`), so `preflight_check_image` checks the local
Docker daemon via the `docker` Python SDK (NOT a CLI subprocess — a real
bug found live: this container's base image installs the `docker.io`
package for the daemon only, no `docker` CLI binary, so a subprocess call
would always fail with "executable not found," never a real answer about
the image); `check_remote_registry_tag_exists` covers the "real registry"
path for a stack that ever grows one.
"""
import os
import sys

import docker
import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import preflight


class _FakeImagesAPI:
    def __init__(self, exists: bool = True, raise_daemon_error: bool = False):
        self._exists = exists
        self._raise_daemon_error = raise_daemon_error

    def get(self, image_ref):
        if self._raise_daemon_error:
            raise docker.errors.DockerException("cannot connect to the Docker daemon")
        if not self._exists:
            raise docker.errors.ImageNotFound(f"no such image: {image_ref}")
        return object()


class _FakeDockerClient:
    def __init__(self, exists: bool = True, raise_daemon_error: bool = False):
        self.images = _FakeImagesAPI(exists=exists, raise_daemon_error=raise_daemon_error)
        self.closed = False

    def close(self):
        self.closed = True


def test_preflight_passes_when_local_image_exists(monkeypatch):
    monkeypatch.setattr(preflight.docker, "from_env", lambda: _FakeDockerClient(exists=True))
    preflight.preflight_check_image("localhost:5001/payments:v1.1.0")  # must not raise


def test_preflight_fails_clearly_when_local_image_missing(monkeypatch):
    monkeypatch.setattr(preflight.docker, "from_env", lambda: _FakeDockerClient(exists=False))
    with pytest.raises(preflight.ImagePreflightError, match="v1.1.0"):
        preflight.preflight_check_image("localhost:5001/payments:v1.1.0")


def test_check_local_docker_image_exists_true(monkeypatch):
    monkeypatch.setattr(preflight.docker, "from_env", lambda: _FakeDockerClient(exists=True))
    assert preflight.check_local_docker_image_exists("localhost:5001/payments:v1.0.0") is True


def test_check_local_docker_image_exists_false_on_missing_tag(monkeypatch):
    monkeypatch.setattr(preflight.docker, "from_env", lambda: _FakeDockerClient(exists=False))
    assert preflight.check_local_docker_image_exists("localhost:5001/payments:does-not-exist") is False


def test_check_local_docker_image_exists_raises_when_daemon_unreachable(monkeypatch):
    """Daemon-unreachable must be distinguishable from image-not-found, not silently reported as False."""
    monkeypatch.setattr(preflight.docker, "from_env", lambda: _FakeDockerClient(raise_daemon_error=True))
    with pytest.raises(preflight.ImagePreflightError, match="Could not reach the local Docker daemon"):
        preflight.check_local_docker_image_exists("localhost:5001/payments:v1.0.0")


def test_check_local_docker_image_exists_closes_the_client(monkeypatch):
    fake_client = _FakeDockerClient(exists=True)
    monkeypatch.setattr(preflight.docker, "from_env", lambda: fake_client)
    preflight.check_local_docker_image_exists("localhost:5001/payments:v1.0.0")
    assert fake_client.closed is True


class _FakeRegistryResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_remote_registry_check_true_when_manifest_found(monkeypatch):
    monkeypatch.setattr(preflight.httpx, "get", lambda *a, **kw: _FakeRegistryResponse(200))
    assert preflight.check_remote_registry_tag_exists("https://registry.example.com", "payments", "v1.1.0") is True


def test_remote_registry_check_false_when_manifest_not_found(monkeypatch):
    monkeypatch.setattr(preflight.httpx, "get", lambda *a, **kw: _FakeRegistryResponse(404))
    assert preflight.check_remote_registry_tag_exists("https://registry.example.com", "payments", "missing-tag") is False


def test_remote_registry_check_raises_on_unauthorized(monkeypatch):
    monkeypatch.setattr(preflight.httpx, "get", lambda *a, **kw: _FakeRegistryResponse(401))
    with pytest.raises(preflight.ImagePreflightError, match="unauthorized"):
        preflight.check_remote_registry_tag_exists("https://registry.example.com", "payments", "v1.1.0")


def test_remote_registry_check_raises_when_unreachable(monkeypatch):
    def _raise(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(preflight.httpx, "get", _raise)
    with pytest.raises(preflight.ImagePreflightError, match="Could not reach registry"):
        preflight.check_remote_registry_tag_exists("https://registry.example.com", "payments", "v1.1.0")


def test_remote_registry_check_raises_on_unexpected_status(monkeypatch):
    monkeypatch.setattr(preflight.httpx, "get", lambda *a, **kw: _FakeRegistryResponse(503))
    with pytest.raises(preflight.ImagePreflightError, match="503"):
        preflight.check_remote_registry_tag_exists("https://registry.example.com", "payments", "v1.1.0")


def test_deploy_task_runs_preflight_before_touching_kubernetes(monkeypatch):
    """The real integration point: deploy_canary_task must reject a missing image before calling the k8s API."""
    from src.tasks import deploy_task

    monkeypatch.setattr(deploy_task, "_load_kube", lambda: (_ for _ in ()).throw(
        AssertionError("must not attempt to load kubeconfig when preflight already failed")
    ))
    monkeypatch.setattr(preflight.docker, "from_env", lambda: _FakeDockerClient(exists=False))

    with pytest.raises(preflight.ImagePreflightError):
        deploy_task.deploy_canary_task("run-1", "v9.9.9-does-not-exist")
