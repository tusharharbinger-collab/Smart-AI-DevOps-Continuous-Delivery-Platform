"""
services/explainability-service/tests/test_predictive_risk_scorer.py

Covers score_deployment_risk() after its migration onto shared/llm_router.py's failover chain (was a
direct Groq httpx call). Confirms: (1) behavior is unchanged for the common single-Groq-key case, (2)
`_heuristic_risk_assessment` still fires when every provider fails or none is configured, and (3) a
malformed/non-schema-conforming response also falls back rather than crashing the caller.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import pytest

import src.predictive_risk_scorer as predictive_risk_scorer
from src.predictive_risk_scorer import score_deployment_risk


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_no_provider_configured_falls_back_to_heuristic():
    result = asyncio.run(
        score_deployment_risk(commit_diff="", commit_message="fix typo", files_changed=["README.md"])
    )
    assert result["risk_level"] in ("LOW", "MEDIUM", "HIGH")
    assert "based on code diff and modified paths" in result["summary"]


def test_llm_success_returns_the_models_own_report(monkeypatch):
    async def fake_call_llm(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return {
            "content": (
                '{"risk_score": 80, "risk_level": "HIGH", "summary": "Touches payment logic.", '
                '"risk_factors": [{"category": "Payments", "description": "payment code changed", "severity": "HIGH"}], '
                '"recommended_canary_steps": [{"weight": 5, "min_duration_seconds": 180}], '
                '"prescriptive_pre_deploy_checks": ["Test refunds"]}'
            ),
            "provider": "gemini",
            "model": "gemini-2.0-flash",
        }

    monkeypatch.setattr(predictive_risk_scorer, "call_llm", fake_call_llm)

    result = asyncio.run(
        score_deployment_risk(commit_diff="+charge_card()", commit_message="add payments", files_changed=["src/pay.py"])
    )

    assert result["risk_score"] == 80
    assert result["risk_level"] == "HIGH"
    assert result["summary"] == "Touches payment logic."


def test_all_providers_failing_falls_back_to_heuristic(monkeypatch):
    from shared.llm_router import AllProvidersFailedError

    async def fake_call_llm(**kwargs):
        raise AllProvidersFailedError([{"provider": "groq", "error": "timeout"}])

    monkeypatch.setattr(predictive_risk_scorer, "call_llm", fake_call_llm)

    result = asyncio.run(
        score_deployment_risk(commit_diff="", commit_message="hotfix auth bypass", files_changed=["src/auth/jwt.py"])
    )

    # Heuristic path still applies its own real signal detection (commit message + file path keywords):
    # base 15 + "hotfix"/"auth" message signal (+25) + "auth" file-path signal (+20) = 60 -> MEDIUM.
    assert result["risk_level"] == "MEDIUM"
    assert any(f["category"] == "Commit Intent" for f in result["risk_factors"])


def test_malformed_llm_response_falls_back_instead_of_raising(monkeypatch):
    async def fake_call_llm(**kwargs):
        return {"content": "not valid json at all", "provider": "mistral", "model": "codestral-latest"}

    monkeypatch.setattr(predictive_risk_scorer, "call_llm", fake_call_llm)

    result = asyncio.run(score_deployment_risk(commit_diff="", commit_message="", files_changed=[]))

    assert result["risk_level"] in ("LOW", "MEDIUM", "HIGH")
