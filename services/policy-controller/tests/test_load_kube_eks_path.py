"""
services/policy-controller/tests/test_load_kube_eks_path.py

Every _load_kube() (actuation_executor.py, cost_tracker.py, and their
pipeline-worker counterparts — all four follow the identical pattern) now
tries shared.eks_auth first. This tests one representative call site
(actuation_executor.py); the other three are the same three-line change and
don't need near-duplicate test files.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from kubernetes import config as k8s_config

from shared import eks_auth
from src import actuation_executor


def test_load_kube_uses_eks_config_when_available(monkeypatch):
    fake_eks_config = {"apiVersion": "v1", "kind": "Config", "current-context": "fake"}
    monkeypatch.setattr(eks_auth, "get_eks_kube_client_config", lambda: fake_eks_config)

    captured = {}
    monkeypatch.setattr(
        k8s_config, "load_kube_config_from_dict", lambda config_dict, **kw: captured.setdefault("config", config_dict)
    )

    def _fail_if_called(*a, **kw):
        raise AssertionError("must not fall back to local/in-cluster kubeconfig when EKS config is available")

    monkeypatch.setattr(k8s_config, "load_incluster_config", _fail_if_called)
    monkeypatch.setattr(k8s_config, "load_kube_config", _fail_if_called)

    actuation_executor._load_kube()

    assert captured["config"] == fake_eks_config


def test_load_kube_falls_back_to_local_when_eks_not_configured(monkeypatch):
    monkeypatch.setattr(eks_auth, "get_eks_kube_client_config", lambda: None)

    calls = []
    monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: calls.append("incluster"))

    actuation_executor._load_kube()

    assert calls == ["incluster"]
