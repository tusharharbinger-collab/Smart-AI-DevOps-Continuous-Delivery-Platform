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
    # Pricing is deliberately not shown at the build step - it belongs after the infrastructure is built.
    assert "$" not in narrative and "Fargate" not in narrative
    assert "No automated tests detected" in narrative and "No lockfile found" in narrative


# ───────── readiness assessment (the score that drives the headline) ─────────

from shared.repo_report import assess_readiness


def _features(**over):
    base = dict(file_count=20, max_depth=3, has_tests=True, has_dockerfile=True, has_lockfile=True, has_ci_config=True,
                has_readme=True, has_license=True, dependency_count=10, language="node", framework=None,
                build_confidence="high")
    base.update(over)
    return RepoFeatures(**base)


def test_a_complete_repo_is_ready_with_no_findings():
    r = assess_readiness(_features())
    assert r["readiness_score"] == 100 and r["risk_level"] == "low" and r["findings"] == []


def test_a_repo_with_a_dockerfile_is_never_scored_zero():
    """The reported bug: a Dockerfile repo with no tests/lockfile/CI showed '0% readiness / HIGH RISK'."""
    r = assess_readiness(_features(has_tests=False, has_lockfile=False, has_ci_config=False, has_license=False))
    assert r["readiness_score"] > 0
    assert "Build method is clear" in r["passed"]


def test_findings_are_ordered_by_importance_and_carry_a_fix():
    r = assess_readiness(_features(has_tests=False, has_license=False, has_lockfile=False))
    assert [f["id"] for f in r["findings"]] == ["tests", "lockfile", "license"] or [f["id"] for f in r["findings"]][0] in ("tests", "lockfile")
    assert r["findings"][-1]["severity"] == "minor"
    assert all(f["why"] and f["fix"] for f in r["findings"])


def test_missing_build_method_is_critical_and_high_risk():
    r = assess_readiness(_features(has_dockerfile=False, build_confidence="low", language=None))
    assert r["findings"][0]["id"] == "build" and r["findings"][0]["severity"] == "critical"
    assert r["risk_level"] == "high"


def test_checks_that_do_not_apply_are_not_held_against_the_repo():
    static = assess_readiness(_features(language="static", dependency_count=0, has_lockfile=False))
    assert all(f["id"] not in ("lockfile", "deps") for f in static["findings"])


def test_missing_only_minor_items_is_medium_at_worst_not_high():
    r = assess_readiness(_features(has_ci_config=False, has_readme=False, has_license=False))
    assert r["risk_level"] != "high" and r["readiness_score"] >= 55


def test_score_repo_risk_uses_readiness_and_keeps_anomaly_separate():
    out = score_repo_risk(_features(has_tests=False, has_lockfile=False))
    assert out["readiness_score"] == assess_readiness(_features(has_tests=False, has_lockfile=False))["readiness_score"]
    assert out["risk_score"] == pytest.approx(1 - out["readiness_score"] / 100, abs=0.001)
    assert "anomaly_score" in out and out["findings"] and out["passed"]


def test_narrative_names_the_top_fix_and_never_mentions_cost(monkeypatch):
    import asyncio
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f = _features(has_tests=False, has_lockfile=False)
    risk = score_repo_risk(f)
    text = asyncio.run(build_narrative(risk["risk_level"], risk["risk_flags"], predict_hosting_cost(f), f,
                                       findings=risk["findings"], readiness_score=risk["readiness_score"]))
    assert "$" not in text and "Fargate" not in text and "/mo" not in text
    assert risk["findings"][0]["fix"] in text and str(risk["readiness_score"]) in text


def test_narrative_lists_problems_not_passed_state_titles(monkeypatch):
    import asyncio
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f = _features(has_tests=False, has_lockfile=False)
    risk = score_repo_risk(f)
    text = asyncio.run(build_narrative(risk["risk_level"], risk["risk_flags"], {}, f, findings=risk["findings"],
                                       readiness_score=risk["readiness_score"]))
    assert "no automated tests" in text and "not pinned" in text and "tests present" not in text


def test_known_language_without_a_start_command_is_important_not_critical():
    r = assess_readiness(_features(has_dockerfile=False, build_confidence="low", language="node"))
    build = next(f for f in r["findings"] if f["id"] == "build")
    assert build["severity"] == "important" and "Start command" in build["problem"] and "node" in build["why"]
    assert r["risk_level"] == "medium"  # setup needed, not a deploy risk


def test_unknown_language_and_no_dockerfile_stays_critical():
    r = assess_readiness(_features(has_dockerfile=False, build_confidence="low", language=None))
    assert next(f for f in r["findings"] if f["id"] == "build")["severity"] == "critical"


def test_test_files_without_a_test_command_get_half_credit_and_their_own_finding():
    full = assess_readiness(_features(has_test_command=True))["readiness_score"]
    partial = assess_readiness(_features(has_test_command=False))
    finding = next(f for f in partial["findings"] if f["id"] == "tests")
    assert finding["severity"] == "minor" and "no test command" in finding["problem"]
    assert "Automated tests present" not in partial["passed"]
    assert 0 < full - partial["readiness_score"] < 20


def test_unknown_test_command_is_not_penalised():
    assert "Automated tests present" in assess_readiness(_features(has_test_command=None))["passed"]
