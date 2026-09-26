"""
services/api-gateway/src/infra_cost.py

Phase 7e - replaces the Infra Architect Agent's own cost figure with an independent one computed from
AWS's Price List API (shared/provisioning/aws_pricing.py, served by pipeline-worker which holds the AWS
credentials). The model's figure is kept, labelled, under `ai_estimated_monthly_cost_usd` / `ai_cost_breakdown`.

Unlike the policy check (src/infra_policy.py, fail-closed), this FAILS SOFT: a cost figure is
informational context for a human, and pricing being unreachable must not stop infrastructure drafting.
But it never fails silently - the proposal is explicitly labelled `ai_estimate_unverified` so nobody
mistakes the model's guess for a computed number.
"""
import json
import os

import httpx
import structlog

logger = structlog.get_logger(__name__)

PIPELINE_WORKER_URL = os.environ.get("PIPELINE_WORKER_URL", "http://pipeline-worker:8001")


def _unverified(proposal: dict, reason: str) -> dict:
    out = dict(proposal)
    out["cost_estimate"] = {"source": "ai_estimate_unverified", "reason": reason}
    return out


async def apply_independent_cost(proposal: dict, region: str) -> dict:
    """Returns a copy of `proposal` with `estimated_monthly_cost_usd`/`cost_breakdown` from AWS prices."""
    try:
        template = json.loads(proposal.get("cloudformation_template") or "{}")
    except json.JSONDecodeError:
        return _unverified(proposal, "template is not valid JSON, so it could not be priced")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/infra-provisioning/estimate-cost",
                json={"template": template, "region": region},
            )
        resp.raise_for_status()
        estimate = resp.json()
    except httpx.HTTPError as e:
        logger.warning("infra_cost_estimate_unavailable", error=str(e) or type(e).__name__)
        return _unverified(proposal, f"pricing service unavailable: {str(e) or type(e).__name__}")

    # Every lookup failed (e.g. missing pricing:GetProducts permission): that is "we don't know", not "free".
    if not estimate["items"] and estimate["errors"] > 0:
        first = next((u["reason"] for u in estimate["unpriced"] if "lookup failed" in u["reason"]), "price lookups failed")
        return _unverified(proposal, first)

    out = dict(proposal)
    out["ai_estimated_monthly_cost_usd"] = proposal.get("estimated_monthly_cost_usd")
    out["ai_cost_breakdown"] = proposal.get("cost_breakdown", [])
    out["estimated_monthly_cost_usd"] = estimate["total_monthly_usd"]
    out["cost_breakdown"] = [
        {"resource": f'{i["resource"]} - {i["basis"]}', "monthly_usd": i["monthly_usd"]} for i in estimate["items"]
    ]
    out["cost_estimate"] = {
        "source": estimate["source"],
        "currency": estimate["currency"],
        "unpriced": estimate["unpriced"],
        "no_fixed_cost": estimate["no_fixed_cost"],
        "assumptions": estimate["assumptions"],
        "errors": estimate["errors"],
    }
    return out
