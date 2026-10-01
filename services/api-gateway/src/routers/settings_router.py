"""
services/api-gateway/src/routers/settings_router.py

Platform LLM-provider settings (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5) - the Gemini -> Groq ->
OpenRouter -> Mistral failover chain every AI-backed feature in this platform is meant to route through
(shared/llm_router.py). This is a thin, role-gated proxy to explainability-service, which owns the real
provider credentials and the actual httpx calls - matching how every other AI-backed endpoint in this
codebase (chatops, log-hygiene, predictive-risk, infra generation) is already proxied through api-gateway
rather than duplicating LLM credentials/logic here. api-gateway itself never holds an LLM provider key.
"""
import os

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.rbac import require_role

router = APIRouter()
logger = structlog.get_logger(__name__)

EXPLAINABILITY_SERVICE_URL = os.environ.get("EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004")


class SetProviderCredentialRequest(BaseModel):
    api_key: str


@router.get("/llm-providers")
async def list_llm_providers():
    """Real status for all four providers (priority, active model, masked credential, configured or not) -
    never exposes a raw credential, mirrors what explainability-service's shared/llm_router.py itself knows."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{EXPLAINABILITY_SERVICE_URL}/llm-providers")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"LLM provider settings service unavailable: {e}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"LLM provider settings service failed: {resp.text}")
    return resp.json()


@router.post("/llm-providers/{name}/test")
async def test_llm_provider(name: str):
    """A real, minimal live request through that provider's own endpoint - never a fabricated "ok"."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{EXPLAINABILITY_SERVICE_URL}/llm-providers/{name}/test")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"LLM provider settings service unavailable: {e}")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Test connection failed: {resp.text}")
    return resp.json()


@router.post("/llm-providers/{name}/credentials")
async def set_llm_provider_credentials(
    name: str,
    body: SetProviderCredentialRequest,
    role: str = Depends(require_role("lead-sre")),
):
    """
    Overrides one provider's API key (stored in Redis by explainability-service, never Postgres, never
    logged - mirrors github_router.py's OAuth-token-in-Redis pattern). Role-gated the same way infra
    approval/execution already is - editing a platform-wide AI credential is not a developer-level action.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{EXPLAINABILITY_SERVICE_URL}/llm-providers/{name}/credentials",
                json={"api_key": body.api_key},
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"LLM provider settings service unavailable: {e}")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")
    if resp.status_code == 422:
        raise HTTPException(status_code=422, detail=resp.json().get("detail", "Invalid credential."))
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Setting credential failed: {resp.text}")
    return resp.json()
