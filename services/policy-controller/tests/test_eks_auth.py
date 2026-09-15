"""
services/policy-controller/tests/test_eks_auth.py

shared/eks_auth.py generates the kubeconfig dict every _load_kube() call
site (actuation_executor.py, cost_tracker.py, and their pipeline-worker
counterparts) now tries first. Never touches a real AWS account — boto3's
STS/EKS clients are monkeypatched.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from shared import eks_auth


@pytest.fixture(autouse=True)
def _clear_eks_env(monkeypatch):
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)


def test_returns_none_when_eks_cluster_name_not_set():
    assert eks_auth.get_eks_kube_client_config() is None


class _FakeSTSClient:
    class meta:
        class service_model:
            service_id = "sts"

    def get_credentials_stub(self):
        return None


class _FakeCredentials:
    access_key = "AKIAFAKE"
    secret_key = "fakesecret"
    token = None


class _FakeSession:
    def __init__(self, region_name=None):
        self.region_name = region_name
        self.events = object()

    def client(self, service_name):
        if service_name == "sts":
            return _FakeSTSClient()
        if service_name == "eks":
            return _FakeEKSClient()
        raise ValueError(f"unexpected client {service_name}")

    def get_credentials(self):
        return _FakeCredentials()


class _FakeEKSClient:
    def describe_cluster(self, name):
        return {
            "cluster": {
                "endpoint": "https://ABCDEF.gr7.us-east-1.eks.amazonaws.com",
                "certificateAuthority": {"data": "ZmFrZS1jYS1kYXRh"},
            }
        }


def test_builds_kubeconfig_dict_when_eks_cluster_name_is_set(monkeypatch):
    monkeypatch.setenv("EKS_CLUSTER_NAME", "smartcd-prod")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(eks_auth.boto3, "session", type("S", (), {"Session": _FakeSession}))
    monkeypatch.setattr(eks_auth, "_generate_eks_token", lambda cluster_name, region, session: "k8s-aws-v1.faketoken")

    config = eks_auth.get_eks_kube_client_config()

    assert config is not None
    assert config["current-context"] == "smartcd-prod"
    assert config["clusters"][0]["cluster"]["server"] == "https://ABCDEF.gr7.us-east-1.eks.amazonaws.com"
    assert config["clusters"][0]["cluster"]["certificate-authority-data"] == "ZmFrZS1jYS1kYXRh"
    assert config["users"][0]["user"]["token"] == "k8s-aws-v1.faketoken"


def test_defaults_region_to_us_east_1_when_not_set(monkeypatch):
    monkeypatch.setenv("EKS_CLUSTER_NAME", "smartcd-prod")
    captured_regions = []

    class _CapturingSession(_FakeSession):
        def __init__(self, region_name=None):
            captured_regions.append(region_name)
            super().__init__(region_name)

    monkeypatch.setattr(eks_auth.boto3, "session", type("S", (), {"Session": _CapturingSession}))
    monkeypatch.setattr(eks_auth, "_generate_eks_token", lambda cluster_name, region, session: "k8s-aws-v1.faketoken")

    eks_auth.get_eks_kube_client_config()

    assert captured_regions == ["us-east-1"]


def test_reraises_when_describe_cluster_fails(monkeypatch):
    monkeypatch.setenv("EKS_CLUSTER_NAME", "smartcd-prod")

    class _FailingEKSClient:
        def describe_cluster(self, name):
            raise RuntimeError("cluster not found")

    class _FailingSession(_FakeSession):
        def client(self, service_name):
            if service_name == "eks":
                return _FailingEKSClient()
            return super().client(service_name)

    monkeypatch.setattr(eks_auth.boto3, "session", type("S", (), {"Session": _FailingSession}))

    with pytest.raises(RuntimeError, match="cluster not found"):
        eks_auth.get_eks_kube_client_config()
