"""
services/api-gateway/tests/test_repo_report_endpoint.py

Integration tests for the /repos/{owner}/{repo}/repo-report and
/repos/{owner}/{repo}/build-detection endpoints in github_router.py.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.routers import github_router as gh_module


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(gh_module.router)

    # Set up dummy redis state on app
    class DummyRedis:
        async def get(self, k):
            return "fake-token"

    app.state.redis = DummyRedis()
    return TestClient(app)


def test_repo_report_endpoint_success(client, monkeypatch):
    async def mock_fetch_repo_tree_and_manifests(request, owner, repo, ref, x_github_token):
        return {
            "file_paths": [
                "package.json",
                "package-lock.json",
                "Dockerfile",
                "README.md",
                "src/index.js",
                "tests/index.test.js",
            ],
            "package_json_content": {
                "name": "sample-app",
                "scripts": {"start": "node src/index.js", "test": "jest"},
                "dependencies": {"express": "^4.21.2"},
            },
            "requirements_txt_content": None,
            "yaml_manifest_content": None,
            "yaml_manifest_path": None,
            "truncated": False,
        }

    monkeypatch.setattr(gh_module, "_fetch_repo_tree_and_manifests", mock_fetch_repo_tree_and_manifests)

    res = client.get("/repos/testowner/testrepo/repo-report?ref=main")
    assert res.status_code == 200
    data = res.json()

    assert "features" in data
    assert "risk" in data
    assert "cost" in data
    assert "narrative" in data
    assert data["truncated"] is False

    features = data["features"]
    assert features["file_count"] == 6
    assert features["has_dockerfile"] is True
    assert features["has_lockfile"] is True
    assert features["has_tests"] is True

    risk = data["risk"]
    assert risk["risk_level"] in ("low", "medium", "high")
    assert 0.0 <= risk["risk_score"] <= 1.0
    assert isinstance(risk["risk_flags"], list)

    cost = data["cost"]
    assert cost["steady_state_monthly_usd"] > 0
    assert cost["task_cpu_units"] == 256
    assert cost["task_memory_mib"] == 512

    assert isinstance(data["narrative"], str)
    assert len(data["narrative"]) > 0


def test_build_detection_endpoint_unaffected(client, monkeypatch):
    async def mock_fetch_repo_tree_and_manifests(request, owner, repo, ref, x_github_token):
        return {
            "file_paths": ["Dockerfile", "main.py"],
            "package_json_content": None,
            "requirements_txt_content": None,
            "yaml_manifest_content": None,
            "yaml_manifest_path": None,
            "truncated": False,
        }

    monkeypatch.setattr(gh_module, "_fetch_repo_tree_and_manifests", mock_fetch_repo_tree_and_manifests)

    res = client.get("/repos/testowner/testrepo/build-detection?ref=main")
    assert res.status_code == 200
    data = res.json()
    assert data["method"] == "dockerfile"
    assert data["dockerfile_path"] == "Dockerfile"
    assert data["suggested_port"] == 8080
    assert data["suggested_health_check_path"] == "/healthz"
