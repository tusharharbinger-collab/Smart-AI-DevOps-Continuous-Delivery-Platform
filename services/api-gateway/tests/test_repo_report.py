"""
services/api-gateway/tests/test_repo_report.py

Unit tests for shared/repo_report.py:
- extract_repo_features: structural metric extraction from file paths
- score_repo_risk: IsolationForest anomaly scoring against reference profiles
- predict_hosting_cost: deterministic ECS Fargate cost math
- build_narrative: deterministic fallback when Groq is unconfigured
"""
import pytest
from shared.repo_report import (
    extract_repo_features,
    score_repo_risk,
    predict_hosting_cost,
    build_narrative,
    RepoFeatures,
)


def test_extract_repo_features_well_structured_node():
    files = [
        "package.json",
        "package-lock.json",
        "Dockerfile",
        "README.md",
        "LICENSE",
        ".github/workflows/ci.yml",
        "src/index.js",
        "tests/index.test.js",
    ]
    pkg_content = {
        "dependencies": {"express": "^4.21.2"},
        "devDependencies": {"jest": "^29.0.0"},
    }
    features = extract_repo_features(files, package_json_content=pkg_content)

    assert features.file_count == 8
    assert features.max_depth == 2
    assert features.has_tests is True
    assert features.has_dockerfile is True
    assert features.has_lockfile is True
    assert features.has_ci_config is True
    assert features.has_readme is True
    assert features.has_license is True
    assert features.dependency_count == 2


def test_extract_repo_features_sparse_python():
    files = [
        "main.py",
        "requirements.txt",
    ]
    reqs_content = "fastapi==0.111.0\nuvicorn==0.30.1\n# comment\n"
    features = extract_repo_features(files, requirements_txt_content=reqs_content)

    assert features.file_count == 2
    assert features.max_depth == 0
    assert features.has_tests is False
    assert features.has_dockerfile is False
    assert features.has_lockfile is False
    assert features.has_ci_config is False
    assert features.has_readme is False
    assert features.has_license is False
    assert features.dependency_count == 2


def test_score_repo_risk_healthy_vs_sparse():
    healthy = RepoFeatures(
        file_count=35,
        max_depth=4,
        has_tests=True,
        has_dockerfile=True,
        has_lockfile=True,
        has_ci_config=True,
        has_readme=True,
        has_license=True,
        dependency_count=15,
        language="node",
        framework="node-server",
        build_confidence="high",
    )
    sparse = RepoFeatures(
        file_count=2,
        max_depth=0,
        has_tests=False,
        has_dockerfile=False,
        has_lockfile=False,
        has_ci_config=False,
        has_readme=False,
        has_license=False,
        dependency_count=0,
        language="python",
        framework=None,
        build_confidence="low",
    )

    score_healthy = score_repo_risk(healthy)
    score_sparse = score_repo_risk(sparse)

    assert 0.0 <= score_healthy["risk_score"] <= 1.0
    assert 0.0 <= score_sparse["risk_score"] <= 1.0
    # Sparse repo should receive a higher or equal anomaly risk score than standard repo
    assert score_sparse["risk_score"] >= score_healthy["risk_score"]

    # Sparse repo must trigger human-explainable risk flags
    assert len(score_sparse["risk_flags"]) > len(score_healthy["risk_flags"])
    assert any("tests" in f.lower() for f in score_sparse["risk_flags"])
    assert any("lockfile" in f.lower() for f in score_sparse["risk_flags"])


def test_predict_hosting_cost_math():
    features = RepoFeatures(
        file_count=10,
        max_depth=2,
        has_tests=True,
        has_dockerfile=True,
        has_lockfile=True,
        has_ci_config=True,
        has_readme=True,
        has_license=True,
        dependency_count=5,
    )
    cost = predict_hosting_cost(features, canary_step_hours=2.0)

    # 256 CPU units (0.25 vCPU) * $0.040478 + 512 MiB (0.5 GiB) * $0.004446
    expected_hourly = (0.25 * 0.040478) + (0.5 * 0.004446)
    expected_monthly = round(expected_hourly * 730.0, 2)
    expected_canary = round(expected_hourly * 2.0, 4)

    assert cost["task_cpu_units"] == 256
    assert cost["task_memory_mib"] == 512
    assert cost["steady_state_monthly_usd"] == expected_monthly
    assert cost["estimated_rollout_window_usd"] == expected_canary
    assert cost["steady_state_monthly_usd"] > 0


def test_build_narrative_fallback_without_api_key(monkeypatch):
    import asyncio
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    features = RepoFeatures(
        file_count=5,
        max_depth=1,
        has_tests=False,
        has_dockerfile=True,
        has_lockfile=False,
        has_ci_config=False,
        has_readme=True,
        has_license=False,
        dependency_count=3,
        language="python",
    )
    cost = predict_hosting_cost(features)
    narrative = asyncio.run(
        build_narrative(
            risk_level="medium",
            risk_flags=["No automated tests detected", "No lockfile found"],
            cost=cost,
            features=features,
        )
    )

    assert isinstance(narrative, str)
    assert f"${cost['steady_state_monthly_usd']}/mo" in narrative
    assert "AWS Fargate" in narrative
