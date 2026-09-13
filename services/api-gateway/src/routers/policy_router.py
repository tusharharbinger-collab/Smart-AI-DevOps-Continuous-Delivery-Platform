"""
services/api-gateway/src/routers/policy_router.py

Screen 3 (Policy & Gates Config, §11.3) backend: validates a pipeline YAML's
`gates`/`guardrails` block against the OPA `delivery.guardrails` package via
a dry-run evaluation, and persists accepted policy snapshots.
"""
import uuid

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.auth.rbac import require_role
from src.config import settings
from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)


class ValidatePolicyRequest(BaseModel):
    policy_yaml: str


class SavePolicyRequest(BaseModel):
    pipeline_id: str
    policy_yaml: str


_DRY_RUN_INPUT_TEMPLATE = {
    "requested_action": "PROMOTE_STEP",
    "verification_verdict": {"status": "HEALTHY", "confidence": 1.0, "timestamp_utc": None},
    "runtime_context": {"current_day": "Monday", "current_time": "10:00", "cluster_maintenance_lock": False},
    "active_step_sample_count": 1_000_000,
    "active_step_duration_seconds": 1_000_000,
    "current_step": {"minSampleSize": 0, "minDuration": "0s"},
    "target_stage": "__dry_run__",
    "approved_signatures": [],
    "cost_analysis": {"delta_percent": 0.0},
}


@router.post("/validate")
async def validate_policy(body: ValidatePolicyRequest):
    """
    Parses gates/guardrails out of the submitted YAML and asks OPA (via a
    synthetic best-case input) whether the policy document itself is
    well-formed and internally consistent — not whether any particular
    verdict would currently be allowed.
    """
    from datetime import datetime, timezone

    try:
        parsed = yaml.safe_load(body.policy_yaml)
        spec = parsed["spec"]
        gates = spec.get("gates", {})
        guardrails = spec.get("guardrails", {})
    except Exception as e:
        return {"valid": False, "errors": [f"YAML parse error: {e}"]}

    dry_run_input = dict(_DRY_RUN_INPUT_TEMPLATE)
    dry_run_input["verification_verdict"]["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    dry_run_input["pipeline_policy"] = {"gates": gates, "guardrails": guardrails}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.OPA_URL}/v1/data/delivery/guardrails",
                json={"input": dry_run_input},
            )
            resp.raise_for_status()
            result = resp.json().get("result", {})
    except Exception as e:
        logger.error("opa_dry_run_failed", error=str(e))
        return {"valid": False, "errors": [f"OPA evaluation unreachable: {e}"]}

    return {
        "valid": True,
        "parsedGates": gates,
        "parsedGuardrails": guardrails,
        "dryRunAllowAction": result.get("allow_action", False),
    }


@router.post("", dependencies=[Depends(require_role("lead-sre"))])
async def save_policy(
    body: SavePolicyRequest, request: Request, db: AsyncSession = Depends(get_request_db)
):
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")

    policy_id = str(uuid.uuid4())
    await db.execute(
        text(
            """
            INSERT INTO policy_rules (policy_id, tenant_id, pipeline_id, rego_snapshot, version)
            VALUES (:policy_id, :tenant_id, :pipeline_id, :snapshot, 1)
            """
        ),
        {
            "policy_id": policy_id,
            "tenant_id": str(tenant_id),
            "pipeline_id": body.pipeline_id,
            "snapshot": body.policy_yaml,
        },
    )
    # Keep the live pipeline row's policy_yaml in sync with the accepted version.
    await db.execute(
        text("UPDATE pipelines SET policy_yaml = :yaml WHERE pipeline_id = :pid AND tenant_id = :tid"),
        {"yaml": body.policy_yaml, "pid": body.pipeline_id, "tid": str(tenant_id)},
    )
    await db.commit()
    logger.info("policy_saved", policy_id=policy_id, pipeline_id=body.pipeline_id)
    return {"policy_id": policy_id, "pipeline_id": body.pipeline_id}


@router.get("/{pipeline_id}")
async def get_policy(pipeline_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = getattr(request.state, "tenant_id", None)
    result = await db.execute(
        text("SELECT policy_yaml FROM pipelines WHERE pipeline_id = :pid AND tenant_id = :tid"),
        {"pid": pipeline_id, "tid": str(tenant_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return {"pipeline_id": pipeline_id, "policy_yaml": row["policy_yaml"]}
