"""
services/api-gateway/src/infra_policy.py

AI_INFRA_PROVISIONING_EXECUTION_PLAN.md 7A / Phase 7f - runs an AI-generated infrastructure proposal
through policies/infra_guardrails.rego on the platform's OPA server.

Why this exists: the "policy checks" the Infra Architect Agent returns are written by the same model
that wrote the infrastructure - it graded its own homework. OPA evaluates the actual CloudFormation
template (the artifact that really gets deployed) with deterministic rules. The model's own list is
kept, clearly labelled, under `ai_self_reported_policy_checks`; the authoritative `policy_checks` are
OPA's.

Fails CLOSED: if OPA cannot be reached, the draft is not created - a proposal nobody independently
checked must not reach a human as if it had been.
"""
import json

import httpx
import structlog
from fastapi import HTTPException

from src.config import settings

logger = structlog.get_logger(__name__)

OPA_INFRA_RESULT_PATH = "/v1/data/infra/guardrails/result"
_STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2}


async def evaluate_infra_policy(proposal: dict, intent_spec: dict, existing_resources: dict | None) -> dict:
    """Returns OPA's result: {"allowed", "deny": [...], "warn": [...], "checks": [...]}."""
    try:
        template = json.loads(proposal.get("cloudformation_template") or "{}")
    except json.JSONDecodeError:
        # infra_generator already rejects invalid template JSON; if one still arrives, block it.
        return {
            "allowed": False,
            "deny": [{"rule": "template_valid", "resource": "template", "severity": "deny",
                      "message": "cloudformation_template is not valid JSON"}],
            "warn": [],
            "checks": [{"check": "template_valid", "description": "Template is valid JSON", "status": "fail",
                        "details": ["cloudformation_template is not valid JSON"]}],
        }

    payload = {"input": {
        "template": template,
        "intent_spec": intent_spec,
        "estimated_monthly_cost_usd": proposal.get("estimated_monthly_cost_usd"),
        "existing_resources": existing_resources or {},
    }}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{settings.OPA_URL}{OPA_INFRA_RESULT_PATH}", json=payload)
        resp.raise_for_status()
        result = resp.json().get("result")
    except httpx.HTTPError as e:
        logger.error("infra_policy_engine_unavailable", error=str(e))
        raise HTTPException(
            status_code=502,
            detail=f"Policy engine unavailable - refusing to present an unchecked proposal: {str(e) or type(e).__name__}",
        )
    if not isinstance(result, dict) or "allowed" not in result:
        # OPA answers 200 with no `result` when the policy isn't loaded - treat that as unavailable, not "allowed".
        raise HTTPException(
            status_code=502,
            detail="Policy engine returned no result for infra.guardrails - is policies/infra_guardrails.rego loaded?",
        )
    return result


def apply_policy_to_proposal(proposal: dict, evaluation: dict) -> dict:
    """
    Returns a copy of `proposal` where `policy_checks` is OPA's independent verdict (shape kept
    compatible with the UI: check/passed/detail, plus status) and the model's own list is preserved
    under `ai_self_reported_policy_checks`.
    """
    checks = sorted(evaluation.get("checks", []), key=lambda c: (_STATUS_ORDER.get(c["status"], 3), c["check"]))
    out = dict(proposal)
    out["ai_self_reported_policy_checks"] = proposal.get("policy_checks", [])
    out["policy_checks"] = [
        {
            "check": c["check"],
            "passed": c["status"] != "fail",
            "status": c["status"],
            "detail": "; ".join(c.get("details") or []) or c.get("description", ""),
        }
        for c in checks
    ]
    out["policy_evaluation"] = {
        "engine": "opa",
        "policy": "infra.guardrails",
        "allowed": bool(evaluation["allowed"]),
        "deny": evaluation.get("deny", []),
        "warn": evaluation.get("warn", []),
    }
    return out


def blocking_messages(proposal: dict | None) -> list[str]:
    """Human-readable hard failures (empty when the proposal may be approved)."""
    evaluation = (proposal or {}).get("policy_evaluation")
    if not evaluation or evaluation.get("allowed", True):
        return []
    return [f["message"] for f in evaluation.get("deny", [])]
