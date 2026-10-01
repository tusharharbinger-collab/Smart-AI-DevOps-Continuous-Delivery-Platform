"""
services/explainability-service/tests/test_ai_features.py

Tests for:
1. Log & Code Hygiene Analyzer (console.log detection, cost estimation, unified diff patch)
2. Predictive Deployment Risk Scorer (Gate 1 AI Risk assessment)
3. Degradation Diagnosis (Verification Inspector AI Diagnostic)
4. HTTP Endpoints in explainability-service
"""
import pytest
from starlette.testclient import TestClient

from src.log_hygiene_analyzer import (
    _scan_code_for_console_logs,
    _generate_deterministic_patch,
    analyze_code_and_log_hygiene,
)
from src.predictive_risk_scorer import (
    _heuristic_risk_assessment,
    score_deployment_risk,
)
from src.report_generator import generate_degradation_diagnosis, _fallback_degradation_diagnosis
from src.main import app


import asyncio


def test_scan_code_for_console_logs():
    code = {
        "src/server.js": (
            "const express = require('express');\n"
            "console.log('Server started on port 3000');\n"
            "app.post('/login', (req, res) => {\n"
            "  console.log('User auth token:', req.body.token);\n"
            "  res.send('ok');\n"
            "});\n"
        ),
        "src/app.py": (
            "import os\n"
            "print('Application initialized')\n"
        ),
    }

    issues = _scan_code_for_console_logs(code)
    assert len(issues) == 3

    # Check sensitive leak detection
    token_issue = [i for i in issues if "token" in i.statement.lower()][0]
    assert token_issue.issue_type == "SENSITIVE_LEAK_RISK"
    assert "credential leak" in token_issue.reason.lower()

    # Real gap found live: the UI only ever showed ONE combined patch for every issue - a human reviewing a
    # single issue had nothing to act on without reading the whole diff. Every issue must carry its own
    # concrete fix, and a sensitive-leak fix must be about masking/removing, never a generic "log it better".
    assert all(i.suggested_fix for i in issues)
    assert "mask" in token_issue.suggested_fix.lower() or "remove" in token_issue.suggested_fix.lower()

    # Check unified diff generation
    patch = _generate_deterministic_patch(code, issues)
    assert patch is not None
    assert "--- a/src/server.js" in patch
    assert "-console.log('Server started on port 3000');" in patch
    assert "-print('Application initialized')" in patch


def test_analyze_code_and_log_hygiene_fallback():
    async def _run():
        code = {
            "index.js": "console.log('debug info');\nconst x = 10;\n"
        }
        result = await analyze_code_and_log_hygiene(code, cloudwatch_logs=["[INFO] 2026-09-23 debug info"])
        assert "detected_issues" in result
        assert len(result["detected_issues"]) == 1
        assert result["estimated_monthly_savings_usd"] >= 0
        assert len(result["recommended_best_practices"]) > 0
        assert result["suggested_patch"] is not None

    asyncio.run(_run())


def test_predictive_risk_scorer_heuristics():
    # High risk commit with migrations and auth
    high_risk = _heuristic_risk_assessment(
        commit_diff="+++ b/migrations/0015_add_table.sql\n+ALTER TABLE users ADD COLUMN secret_hash text;\n" * 20,
        commit_message="Breaking migration: refactor auth security schema",
        files_changed=["migrations/0015_add_table.sql", "src/auth/jwt.py"],
    )
    assert high_risk["risk_score"] >= 70
    assert high_risk["risk_level"] in ("HIGH", "CRITICAL")
    assert any(rf["category"] == "Database Schema" for rf in high_risk["risk_factors"])
    assert any(rf["category"] == "Security / Auth" for rf in high_risk["risk_factors"])
    # Schedule should be cautious
    assert high_risk["recommended_canary_steps"][0]["weight"] <= 10

    # Low risk commit
    low_risk = _heuristic_risk_assessment(
        commit_diff="+++ b/README.md\n+Updated docs\n",
        commit_message="docs: update readme typo",
        files_changed=["README.md"],
    )
    assert low_risk["risk_score"] < 40
    assert low_risk["risk_level"] == "LOW"


def test_degradation_diagnosis_fallback():
    async def _run():
        data = {
            "service_name": "payments-service",
            "final_verdict": "DEGRADED",
            "confidence": 0.65,
            "sample_size": 42,
            "error_logs": ["WARN: slow query on /checkout taking 850ms"],
        }
        diagnosis = await generate_degradation_diagnosis(data)
        assert "executive_summary" in diagnosis
        assert "DEGRADED" in diagnosis["executive_summary"]
        assert "ways_to_improve" in diagnosis
        assert len(diagnosis["ways_to_improve"]) >= 2
        assert "Sample Size Under Floor" in diagnosis["degradation_reason"]

    asyncio.run(_run())


def test_explainability_http_endpoints():
    client = TestClient(app)

    # 1. Log hygiene endpoint
    resp = client.post(
        "/log-hygiene",
        json={"code_files": {"app.js": "console.log('test');\n"}, "cloudwatch_logs": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "detected_issues" in data
    assert len(data["detected_issues"]) == 1

    # 2. Predictive risk endpoint
    resp = client.post(
        "/predictive-risk",
        json={
            "commit_diff": "diff --git a/app.py b/app.py\n+print('hello')",
            "commit_message": "test commit",
            "files_changed": ["app.py"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "risk_score" in data
    assert "recommended_canary_steps" in data

    # 3. Degradation diagnosis endpoint
    resp = client.post(
        "/degradation-diagnosis",
        json={"service_name": "test-svc", "confidence": 0.72, "sample_size": 150},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "executive_summary" in data
    assert "ways_to_improve" in data

    # 4. LLM provider settings (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5)
    resp = client.get("/llm-providers")
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    assert [p["name"] for p in providers] == ["gemini", "groq", "openrouter", "mistral"]

    # 5. "Add a component" catalog + compatibility check (§3.2, Phase C)
    resp = client.get("/component-catalog")
    assert resp.status_code == 200
    assert any(e["resource_type"] == "ec2_instance" for e in resp.json()["catalog"])

    resp = client.post(
        "/check-component",
        json={
            "resource_type": "ec2_instance",
            "params": {"instance_type": "t3.micro"},
            "archetype": "stateless_web_service",
            "deploy_target": "aws_ecs",
            "intent_spec": {},
        },
    )
    assert resp.status_code == 200
    check = resp.json()
    assert check["compatible"] is True
    assert check["instruction"] == "Add an EC2 instance with the following configuration: instance_type=t3.micro."
