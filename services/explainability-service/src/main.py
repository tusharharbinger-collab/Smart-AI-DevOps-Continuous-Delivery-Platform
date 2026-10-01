"""
services/explainability-service/src/main.py

FastAPI entrypoint for the Explainability Service: Groq-backed RCA (§9.2),
exact-citation evidence (§9.1), the Action Decision Report (§12.2), and the
Delivery-Health Digest (§12.3).
"""
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from shared.component_catalog import build_add_component_instruction, check_component_compatibility, list_component_catalog
from shared.llm_router import list_providers, set_provider_credential_override
from shared.llm_router import test_provider_connection as check_provider_connection
from shared.logging_config import configure_logging

configure_logging("explainability-service")

import os

import structlog

from src.chatops_answerer import answer_chatops_question
from src.chatops_context import assemble_chatops_context
from src.citation_builder import build_citations_from_engine_evidence
from src.db import get_db
from src.decision_report import build_decision_report
from src.digest_generator import generate_delivery_health_digest
from src.digest_summarizer import generate_digest_summary
from src.health_router import router as health_router
from src.infra_generator import InfraGenerationError, generate_infra_proposal
from src.pipeline_generator import PipelineGenerationError, generate_pipeline_yaml
from src.report_generator import generate_rca, generate_degradation_diagnosis
from src.stage_failure_analyzer import generate_stage_failure_rca
from src.copilot_engine import CopilotConverseRequest, CopilotConverseResponse, converse_with_copilot
from src.log_hygiene_analyzer import analyze_code_and_log_hygiene
from src.predictive_risk_scorer import score_deployment_risk


logger = structlog.get_logger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("explainability_service_startup")
    yield
    await app.state.redis.aclose()
    logger.info("explainability_service_shutdown")


app = FastAPI(title="Explainability Service", version="1.0.0", lifespan=lifespan)
app.include_router(health_router, tags=["health"])
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.post("/rca")
async def post_generate_rca(analysis_data: dict):
    """Generates a grounded RCA via Groq (with deterministic fallback)."""
    return await generate_rca(analysis_data)


@app.post("/citations")
async def post_build_citations(evidence: dict):
    """Builds the exact-citation string set from a verification-engine evidence dict."""
    return {"citation": build_citations_from_engine_evidence(evidence)}


@app.post("/decision-report")
async def post_decision_report(body: dict):
    """
    body: {"pipeline_run_id": str, "verdict": dict, "opa_result": dict, "action_taken": str}
    """
    try:
        report = build_decision_report(
            pipeline_run_id=body["pipeline_run_id"],
            verdict=body["verdict"],
            opa_result=body["opa_result"],
            action_taken=body["action_taken"],
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")
    return report.to_dict()


@app.post("/generate-pipeline")
async def post_generate_pipeline(body: dict):
    """
    body: {"prompt": str, "current_yaml": str, "context": dict, "validation_error": str | None}
    Never auto-applies anything — the caller (api-gateway) is responsible for
    validating the result against pipeline-worker's real validator before it
    ever reaches a human's screen for review.
    """
    try:
        return await generate_pipeline_yaml(
            prompt=body["prompt"],
            current_yaml=body["current_yaml"],
            context=body.get("context", {}),
            validation_error=body.get("validation_error"),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")
    except PipelineGenerationError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/generate-infra")
async def post_generate_infra(body: dict):
    """
    body: {"intent_spec": dict, "archetype": str}

    Phase 4's Infra Architect Agent — proposes a topology + Terraform HCL +
    cost estimate + policy checks for the ALREADY-MATCHED golden-path
    archetype (Phase 1). Never auto-applies: the caller (api-gateway) runs
    the result through the tiered deployment-readiness gate (Phase 3) and
    a human approval checkpoint before anything provisions.
    """
    try:
        return await generate_infra_proposal(
            intent_spec=body["intent_spec"],
            archetype=body["archetype"],
            # AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md: both optional. existing_resources =
            # {slot: {"id", "details"}} (verified against AWS by the caller); edit =
            # {"current_proposal": dict, "instruction": str} for prompt-driven edits.
            existing_resources=body.get("existing_resources") or None,
            edit=body.get("edit") or None,
            # Extras-only mode (shared/infra_needs.py): what the platform does NOT already build, plus the real network.
            additions=body.get("additions") or None,
            platform_context=body.get("platform_context") or None,
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")
    except InfraGenerationError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/infra-failure-rca")
async def post_infra_failure_rca(body: dict):
    """body: {"draft_id": str, "phase": "change_set"|"stack", "status_reason": str|None, "events": list[dict], "resources": list[dict]}"""
    from src.infra_failure_analyzer import analyze_infra_failure

    return await analyze_infra_failure(
        draft_id=str(body.get("draft_id", "")),
        phase=body.get("phase", "stack"),
        status_reason=body.get("status_reason"),
        events=body.get("events") or [],
        resources=body.get("resources") or [],
    )


@app.post("/stage-failure-rca")
async def post_stage_failure_rca(body: dict):
    """body: {"run_id": str, "failed_stage": str, "error_message": str, "recent_logs": list[str]}"""
    try:
        return await generate_stage_failure_rca(
            run_id=body["run_id"],
            failed_stage=body["failed_stage"],
            error_message=body["error_message"],
            recent_logs=body.get("recent_logs", []),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")


@app.get("/digest/{tenant_id}")
async def get_digest(tenant_id: str, days: int = 7, db: AsyncSession = Depends(get_db)):
    # Must run in the same transaction as the queries digest_generator.py
    # issues below — set_config(..., true) ("SET LOCAL" semantics) only
    # lasts for the current transaction, so this can't be wrapped in its own
    # `async with db.begin():` block (that would commit and close the
    # transaction before the real queries ever ran).
    from sqlalchemy import text

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    digest = await generate_delivery_health_digest(db, tenant_id, days)
    # AI digest summary (P0, 2026-09-16) — grounded entirely in the numbers
    # digest_generator.py just computed above, never a second data source.
    # Wrapped so a Groq outage degrades the summary quality, never the
    # digest response itself (generate_digest_summary already guarantees
    # this internally via its own fallback, this is defense in depth).
    try:
        digest["ai_summary"] = await generate_digest_summary(digest)
    except Exception as e:
        logger.warning("digest_ai_summary_failed", tenant_id=tenant_id, error=str(e))
        digest["ai_summary"] = None
    return digest


@app.post("/chatops/ask")
async def post_chatops_ask(body: dict, db: AsyncSession = Depends(get_db)):
    """
    ChatOps query interface — the assignment's own named bonus item.
    body: {"tenant_id": str, "project_id": str, "question": str}. Same
    same-transaction SET LOCAL requirement as /digest/{tenant_id} above —
    assemble_chatops_context's queries must run under the same RLS scoping
    this sets.
    """
    from sqlalchemy import text

    tenant_id = body.get("tenant_id")
    project_id = body.get("project_id")
    question = body.get("question")
    if not tenant_id or not project_id or not question:
        raise HTTPException(status_code=422, detail="tenant_id, project_id, and question are required")

    await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
    context = await assemble_chatops_context(db, tenant_id, project_id)
    context["project_id"] = project_id
    return await answer_chatops_question(context, question)


@app.post("/copilot/converse", response_model=CopilotConverseResponse)
async def post_copilot_converse(body: CopilotConverseRequest):
    """
    Multi-turn AI DevOps Copilot & UI Guide endpoint.
    Maintains zero persistent state in DB (context stored strictly in that chat session).
    Enforces strict scope guardrails, secret scrubbing, and UI/DevOps knowledge.
    """
    return await converse_with_copilot(
        messages=body.messages,
        project_context=body.project_context,
        wizard_context=body.wizard_context,
    )


@app.post("/degradation-diagnosis")
async def post_degradation_diagnosis(body: dict):
    """
    Analyzes why a canary verdict is DEGRADED and produces prescriptive recommendations & code patches.
    body: {"service_name": str, "confidence": float, "metric_evidence": list, "error_logs": list, "commit_diff": str}
    """
    return await generate_degradation_diagnosis(body)


@app.post("/log-hygiene")
async def post_log_hygiene(body: dict):
    """
    Scans code files and CloudWatch logs for noisy console/print statements and security leaks.
    body: {"code_files": dict[str, str], "cloudwatch_logs": list[str]}
    """
    code_files = body.get("code_files", {})
    cloudwatch_logs = body.get("cloudwatch_logs", [])
    return await analyze_code_and_log_hygiene(code_files, cloudwatch_logs)


def _redis_or_none(request: Request):
    """
    `app.state.redis` is only set once the lifespan context manager has actually run - a TestClient used
    without `with TestClient(app) as client:` (several existing tests in this suite predate this) never
    triggers it. Every caller here treats a missing Redis client the same way shared/llm_router.py already
    treats a Redis error: fall back to env-var-only credential resolution, never crash the request.
    """
    return getattr(request.app.state, "redis", None)


@app.post("/predictive-risk")
async def post_predictive_risk(body: dict, request: Request):
    """
    Performs pre-flight deployment risk assessment at Gate 1.
    body: {"commit_diff": str, "commit_message": str, "files_changed": list[str]}
    """
    return await score_deployment_risk(
        commit_diff=body.get("commit_diff"),
        commit_message=body.get("commit_message"),
        files_changed=body.get("files_changed"),
        redis_client=_redis_or_none(request),
    )


# ─────────────────────────── LLM provider settings ───────────────────────────
# AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5 - the Gemini -> Groq -> OpenRouter -> Mistral failover
# chain every AI call in this service goes through (shared/llm_router.py). This service owns the actual
# provider credentials/config; api-gateway's settings_router.py is a thin, role-gated proxy in front of
# these three endpoints, matching how every other AI-backed feature in this codebase is proxied.


class SetLLMProviderCredentialRequest(BaseModel):
    api_key: str


@app.get("/llm-providers")
async def get_llm_providers(request: Request):
    return {"providers": await list_providers(_redis_or_none(request))}


@app.post("/llm-providers/{name}/test")
async def post_test_llm_provider(name: str, request: Request):
    try:
        return await check_provider_connection(name, _redis_or_none(request))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/llm-providers/{name}/credentials")
async def post_set_llm_provider_credentials(name: str, body: SetLLMProviderCredentialRequest, request: Request):
    redis_client = _redis_or_none(request)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis is unavailable - cannot persist a credential override right now.")
    try:
        await set_provider_credential_override(name, body.api_key, redis_client)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


# ─────────────────────────── "Add a component" catalog + compatibility check ───────────────────────────
# AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2 (Phase C) - shared/component_catalog.py's rule table
# (LLM fallback only for a resource type outside the catalog). Deliberately a SEPARATE endpoint from
# /generate-infra: "just checking" must never trigger a full proposal regeneration.


@app.get("/component-catalog")
async def get_component_catalog():
    return {"catalog": list_component_catalog()}


@app.post("/check-component")
async def post_check_component(body: dict, request: Request):
    """
    body: {"resource_type": str, "params": dict, "archetype": str, "deploy_target": str, "intent_spec": dict}
    """
    try:
        resource_type = body["resource_type"]
        params = body.get("params") or {}
        result = await check_component_compatibility(
            resource_type=resource_type,
            params=params,
            archetype=body["archetype"],
            deploy_target=body["deploy_target"],
            intent_spec=body.get("intent_spec") or {},
            redis_client=_redis_or_none(request),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing field: {e}")
    # The ready-to-send free-text instruction the frontend hands to the EXISTING /edit endpoint on confirm -
    # computed here (not by the frontend) so there is exactly one place that knows how to phrase it.
    return {**result, "instruction": build_add_component_instruction(resource_type, params)}


