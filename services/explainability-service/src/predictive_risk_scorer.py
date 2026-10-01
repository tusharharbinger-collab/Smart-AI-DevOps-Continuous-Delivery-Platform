"""
services/explainability-service/src/predictive_risk_scorer.py

Pre-Flight AI Predictive Risk Scorer (Gate 1 AI Risk Assessment).
Analyzes commit diffs, modified file types, and architectural touchpoints to:
1. Compute a Deployment Risk Score (0 - 100).
2. Highlight high-risk code changes (auth, database, payment, network, or large diffs).
3. Recommend an adaptive progressive canary ramp schedule tailored to risk.
4. Output prescriptive pre-deploy checks.

Runs through shared/llm_router.py's Gemini -> Groq -> OpenRouter -> Mistral failover chain for deep
semantic reasoning, with a deterministic heuristic fallback if every configured provider fails.
"""
import json
import re
from typing import Any

import structlog
from pydantic import BaseModel, Field

from shared.llm_router import AllProvidersFailedError, call_llm

logger = structlog.get_logger(__name__)

_RISK_SCORER_SYSTEM_PROMPT = (
    "You are an expert AI Principal Site Reliability Engineer (AI SRE). "
    "Your role is to assess the deployment risk of a new code release BEFORE it reaches production traffic. "
    "Analyze the provided Git commit diff, commit message, and changed files. "
    "Identify high-risk changes such as schema migrations, authentication or authorization modifications, "
    "payment logic, concurrency primitives, network timeouts, or unhandled exceptions. "
    "Compute a risk_score from 0 (completely safe / trivial doc/copy change) to 100 (catastrophic risk / breaking change). "
    "Recommend an adaptive progressive canary schedule (e.g. step percentages and dwell durations in seconds). "
    "Respond ONLY with a valid JSON object matching this schema: "
    "risk_score (integer, 0-100), "
    "risk_level (string: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'), "
    "summary (string, two-sentence explanation of release risk), "
    "risk_factors (array of {category: string, description: string, severity: 'LOW' | 'MEDIUM' | 'HIGH'}), "
    "recommended_canary_steps (array of {weight: integer, min_duration_seconds: integer}), "
    "prescriptive_pre_deploy_checks (array of strings, specific tests or checks recommended before rollout)."
)


class RiskFactor(BaseModel):
    category: str
    description: str
    severity: str = "MEDIUM"


class CanaryStepRecommendation(BaseModel):
    weight: int
    min_duration_seconds: int


class PredictiveRiskReport(BaseModel):
    risk_score: int = Field(ge=0, le=100)
    risk_level: str = "MEDIUM"
    summary: str
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    recommended_canary_steps: list[CanaryStepRecommendation] = Field(default_factory=list)
    prescriptive_pre_deploy_checks: list[str] = Field(default_factory=list)


def _heuristic_risk_assessment(commit_diff: str | None, commit_message: str | None, files_changed: list[str] | None) -> dict[str, Any]:
    """
    Deterministic fallback when every configured LLM provider is unreachable, or none is configured at all.
    """
    score = 15
    factors: list[RiskFactor] = []
    diff_text = commit_diff or ""
    msg = (commit_message or "").lower()
    files = files_changed or []

    # Check commit message signals
    if any(k in msg for k in ["breaking", "migration", "refactor", "security", "auth", "hotfix"]):
        score += 25
        factors.append(RiskFactor(category="Commit Intent", description=f"Commit message signals high-risk change: '{commit_message}'", severity="HIGH"))

    # File path heuristics
    for f in files:
        f_lower = f.lower()
        if any(db_kw in f_lower for db_kw in ["migration", "schema", "alembic", "db/"]):
            score += 30
            factors.append(RiskFactor(category="Database Schema", description=f"Database migration file modified: {f}", severity="HIGH"))
        elif any(sec_kw in f_lower for sec_kw in ["auth", "jwt", "rbac", "security", "token"]):
            score += 20
            factors.append(RiskFactor(category="Security / Auth", description=f"Security/Auth layer modified: {f}", severity="HIGH"))
        elif any(net_kw in f_lower for net_kw in ["dockerfile", "k8s", "alb", "gateway", "proxy"]):
            score += 15
            factors.append(RiskFactor(category="Infrastructure", description=f"Infrastructure/Networking config modified: {f}", severity="MEDIUM"))

    # Diff size heuristic
    lines_added = len(re.findall(r"^\+[^+]", diff_text, re.MULTILINE))
    lines_deleted = len(re.findall(r"^-[^-]", diff_text, re.MULTILINE))
    if lines_added + lines_deleted > 300:
        score += 20
        factors.append(RiskFactor(category="Diff Volume", description=f"Large diff ({lines_added} lines added, {lines_deleted} lines removed)", severity="MEDIUM"))

    score = min(max(score, 5), 95)
    
    if score >= 75:
        level = "HIGH"
        steps = [
            CanaryStepRecommendation(weight=5, min_duration_seconds=180),
            CanaryStepRecommendation(weight=15, min_duration_seconds=180),
            CanaryStepRecommendation(weight=30, min_duration_seconds=180),
            CanaryStepRecommendation(weight=50, min_duration_seconds=120),
            CanaryStepRecommendation(weight=100, min_duration_seconds=60),
        ]
    elif score >= 40:
        level = "MEDIUM"
        steps = [
            CanaryStepRecommendation(weight=10, min_duration_seconds=120),
            CanaryStepRecommendation(weight=25, min_duration_seconds=120),
            CanaryStepRecommendation(weight=50, min_duration_seconds=90),
            CanaryStepRecommendation(weight=100, min_duration_seconds=60),
        ]
    else:
        level = "LOW"
        steps = [
            CanaryStepRecommendation(weight=20, min_duration_seconds=60),
            CanaryStepRecommendation(weight=50, min_duration_seconds=60),
            CanaryStepRecommendation(weight=100, min_duration_seconds=30),
        ]

    return {
        "risk_score": score,
        "risk_level": level,
        "summary": f"Calculated deployment risk score of {score}/100 ({level} risk) based on code diff and modified paths.",
        "risk_factors": [f.model_dump() for f in factors],
        "recommended_canary_steps": [s.model_dump() for s in steps],
        "prescriptive_pre_deploy_checks": [
            "Confirm unit tests pass in Gate 1 build dry-run.",
            "Verify backwards compatibility for any modified API schemas.",
            "Monitor canary P95 latency and error rate for the first 120 seconds.",
        ],
    }


async def score_deployment_risk(
    commit_diff: str | None = None,
    commit_message: str | None = None,
    files_changed: list[str] | None = None,
    timeout_seconds: float = 30.0,
    redis_client=None,
) -> dict[str, Any]:
    """
    Computes pre-flight deployment risk score and recommended canary ramp schedule.

    Routed through shared/llm_router.py's Gemini -> Groq -> OpenRouter -> Mistral failover chain (see
    AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5) instead of calling Groq directly - behavior is
    unchanged for a deployment with only GROQ_API_KEY set (the previous and still-common case), since an
    unconfigured provider is silently skipped by the router. `_heuristic_risk_assessment` remains the final,
    deterministic fallback when EVERY configured provider fails (or none are configured at all) - this
    router never invents a fallback of its own.
    """
    diff_text = commit_diff or ""
    files = files_changed or []

    analysis_input = {
        "commit_message": commit_message,
        "files_changed": files,
        "commit_diff": diff_text[:5000] if len(diff_text) > 5000 else diff_text,
    }

    prompt = (
        f"Perform a pre-flight deployment risk assessment for this release:\n"
        f"{json.dumps(analysis_input, indent=2)}"
    )

    try:
        result = await call_llm(
            messages=[
                {"role": "system", "content": _RISK_SCORER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            timeout_seconds=timeout_seconds,
            redis_client=redis_client,
        )
        parsed = PredictiveRiskReport.model_validate_json(result["content"])
        return parsed.model_dump()
    except AllProvidersFailedError as exc:
        logger.info("predictive_risk_using_heuristic_fallback", reason=str(exc))
        return _heuristic_risk_assessment(commit_diff, commit_message, files)
    except Exception as exc:
        logger.error("predictive_risk_llm_response_invalid", error=str(exc))
        return _heuristic_risk_assessment(commit_diff, commit_message, files)
