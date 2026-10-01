"""
services/pipeline-worker/tests/test_ecr_auth.py

get_ecr_registry_credential feeds build_task.py::_push_to_registry's
existing generic {"username", "secret"} shape with a real ECR token,
so the registry-push path that already exists for any project declaring
`image_name` + `registry_credential` fires automatically for an ECR image
with no manually-pasted credential. Never touches a real AWS account —
boto3's ECR client is monkeypatched.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.tasks import ecr_auth


@pytest.mark.parametrize(
    "image_name,expected",
    [
        ("123456789012.dkr.ecr.us-east-1.amazonaws.com/payments", True),
        ("registry.internal/orders-api", False),
        ("localhost:5001/payments", False),
        ("ghcr.io/acme/payments", False),
    ],
)
def test_is_ecr_image_detects_ecr_uri_pattern(image_name, expected):
    assert ecr_auth.is_ecr_image(image_name) is expected


class _FakeECRClient:
    def __init__(self, token: str):
        self._token = token

    def get_authorization_token(self):
        return {"authorizationData": [{"authorizationToken": self._token}]}


def _b64_token(username: str, secret: str) -> str:
    return base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("utf-8")


def test_get_ecr_registry_credential_decodes_username_and_secret(monkeypatch):
    fake_token = _b64_token("AWS", "some-temporary-ecr-token")
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: _FakeECRClient(fake_token))

    credential = ecr_auth.get_ecr_registry_credential("us-east-1")

    assert credential == {"username": "AWS", "secret": "some-temporary-ecr-token"}


def test_get_ecr_registry_credential_passes_region_through(monkeypatch):
    captured = {}

    def _fake_client(service, region_name=None):
        captured["service"] = service
        captured["region_name"] = region_name
        return _FakeECRClient(_b64_token("AWS", "token"))

    monkeypatch.setattr(ecr_auth.boto3, "client", _fake_client)

    ecr_auth.get_ecr_registry_credential("eu-west-1")

    assert captured == {"service": "ecr", "region_name": "eu-west-1"}


class _RepositoryAlreadyExistsException(Exception):
    pass


class _FakeECRClientForRepoCreation:
    """Real gap found live: ECR (unlike most registries) never auto-creates
    a repository on first push — a project's first-ever build failed the
    push outright until someone had already run `aws ecr create-repository`
    by hand. ensure_ecr_repository_exists closes that gap; this stand-in
    mirrors the real client's `.exceptions.RepositoryAlreadyExistsException`
    attribute access pattern (boto3 generates that class per-client, not as
    a plain importable exception)."""

    exceptions = type("Exceptions", (), {"RepositoryAlreadyExistsException": _RepositoryAlreadyExistsException})

    def __init__(self, already_exists: bool = False):
        self.already_exists = already_exists
        self.create_calls = []

    def create_repository(self, repositoryName):
        self.create_calls.append(repositoryName)
        if self.already_exists:
            raise self.exceptions.RepositoryAlreadyExistsException()


def test_ensure_ecr_repository_exists_creates_a_new_repo(monkeypatch):
    fake = _FakeECRClientForRepoCreation(already_exists=False)
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: fake)

    ecr_auth.ensure_ecr_repository_exists("123456789012.dkr.ecr.us-east-1.amazonaws.com/payments", "us-east-1")

    assert fake.create_calls == ["payments"]


def test_ensure_ecr_repository_exists_is_idempotent_when_already_present(monkeypatch):
    fake = _FakeECRClientForRepoCreation(already_exists=True)
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: fake)

    # Must not raise — an already-existing repo is the normal case for
    # every build after a project's first one.
    ecr_auth.ensure_ecr_repository_exists("123456789012.dkr.ecr.us-east-1.amazonaws.com/payments", "us-east-1")

    assert fake.create_calls == ["payments"]


def test_ensure_ecr_repository_exists_extracts_the_repo_name_from_the_full_uri(monkeypatch):
    fake = _FakeECRClientForRepoCreation()
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: fake)

    ecr_auth.ensure_ecr_repository_exists("236087863083.dkr.ecr.us-east-1.amazonaws.com/cicd-test", "us-east-1")

    assert fake.create_calls == ["cicd-test"]


# ───────── delete_ecr_repository (project-deletion teardown) ─────────


class _RepositoryNotFoundException(Exception):
    pass


class _FakeECRClientForRepoDeletion:
    """Real gap found live: a deleted project's build image was left in ECR forever, still billing
    storage. Mirrors the real client's `.exceptions.RepositoryNotFoundException` attribute-access pattern,
    same as `_FakeECRClientForRepoCreation` does for its own exception."""

    exceptions = type("Exceptions", (), {"RepositoryNotFoundException": _RepositoryNotFoundException})

    def __init__(self, already_deleted: bool = False):
        self.already_deleted = already_deleted
        self.delete_calls = []

    def delete_repository(self, repositoryName, force=False):
        self.delete_calls.append((repositoryName, force))
        if self.already_deleted:
            raise self.exceptions.RepositoryNotFoundException()


def test_delete_ecr_repository_force_deletes_by_name(monkeypatch):
    fake = _FakeECRClientForRepoDeletion()
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: fake)

    result = ecr_auth.delete_ecr_repository("123456789012.dkr.ecr.us-east-1.amazonaws.com/payments", "us-east-1")

    assert fake.delete_calls == [("payments", True)]
    assert result == {"status": "deleted", "repository": "payments"}


def test_delete_ecr_repository_strips_a_tag_if_one_was_passed(monkeypatch):
    fake = _FakeECRClientForRepoDeletion()
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: fake)

    ecr_auth.delete_ecr_repository("123456789012.dkr.ecr.us-east-1.amazonaws.com/payments:v1.1.0", "us-east-1")

    assert fake.delete_calls == [("payments", True)]


def test_delete_ecr_repository_already_gone_is_a_no_op_not_an_error(monkeypatch):
    fake = _FakeECRClientForRepoDeletion(already_deleted=True)
    monkeypatch.setattr(ecr_auth.boto3, "client", lambda service, region_name=None: fake)

    result = ecr_auth.delete_ecr_repository("123456789012.dkr.ecr.us-east-1.amazonaws.com/payments", "us-east-1")

    assert result == {"status": "already_deleted", "repository": "payments"}


def test_delete_ecr_repository_on_a_non_ecr_image_is_a_no_op(monkeypatch):
    # A project's container_image on a registry the platform never created (or a legacy non-ECR project)
    # has nothing for this to delete - must never attempt a boto3 call against a name that isn't an ECR repo.
    called = False

    def _fail_if_called(*a, **kw):
        nonlocal called
        called = True

    monkeypatch.setattr(ecr_auth.boto3, "client", _fail_if_called)

    result = ecr_auth.delete_ecr_repository("registry.internal/orders-api", "us-east-1")

    assert result == {"status": "not_an_ecr_image"}
    assert called is False
