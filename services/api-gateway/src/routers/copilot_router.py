"""
services/api-gateway/src/routers/copilot_router.py

API Gateway router for the AI DevOps Copilot & UI Guide.
- Enforces tenant authentication and RLS isolation.
- Optionally hydrates project metadata if a project_id is provided.
- Passes conversation history (strictly in-memory, zero DB persistence).
- Proxies to explainability-service (/copilot/converse).
- Defense-in-depth secret scrubbing on all responses.
"""
import os
import re
from typing import Any, Dict, List, Optional
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

EXPLAINABILITY_SERVICE_URL = os.environ.get(
    "EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004"
)

# Regex patterns for defense-in-depth secret scrubbing
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key ID
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
    re.compile(r"eyJ[A-Za-z0-9-_=]+\.eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]*"),  # JWT
    re.compile(r"postgres(?:ql)?://[^\s:]+:[^\s@]+@[^\s/]+/[^\s]+"),  # DB connection string
    re.compile(r"gsk_[a-zA-Z0-9]{32,}"),  # Groq API key
]


class ChatMessage(BaseModel):
    role: str
    content: str


class CopilotChatRequest(BaseModel):
    messages: List[ChatMessage]
    project_id: Optional[str] = None
    wizard_context: Optional[Dict[str, Any]] = None


class CopilotChatResponse(BaseModel):
    reply: str
    suggested_actions: List[str] = Field(default_factory=list)


def _get_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


def sanitize_secrets(text: str) -> str:
    sanitized = text
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
    return sanitized


async def _inspect_github_repo(request: Request, owner: str, repo: str, branch: str = "main") -> Optional[Dict[str, Any]]:
    """Inspects target GitHub repo structure and manifests for live Copilot context."""
    try:
        from src.routers.github_router import _fetch_repo_tree_and_manifests
        from shared.repo_scanner import detect_build_method, suggest_networking_defaults

        fetch_res = await _fetch_repo_tree_and_manifests(request, owner, repo, branch, None)
        file_paths = fetch_res.get("file_paths", [])
        if not file_paths:
            return None

        detection = detect_build_method(
            file_paths,
            package_json_content=fetch_res.get("package_json_content"),
            yaml_manifest_content=fetch_res.get("yaml_manifest_content"),
            yaml_manifest_path=fetch_res.get("yaml_manifest_path"),
            requirements_txt_content=fetch_res.get("requirements_txt_content"),
            procfile_content=fetch_res.get("procfile_content"),
        )
        networking = suggest_networking_defaults(detection)

        pkg = fetch_res.get("package_json_content") or {}
        scripts = pkg.get("scripts", {})
        main_entry = pkg.get("main", "")
        infra = detection.infra_signals

        return {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "file_paths_sample": file_paths[:25],
            "language": detection.language,
            "framework": detection.framework,
            "method": detection.method,
            "start_command": detection.start_command,
            "test_command": detection.test_command,
            "dockerfile_path": detection.dockerfile_path,
            "archetype": detection.archetype,
            "suggested_port": networking.get("suggested_port", 8080),
            "suggested_health_check_path": networking.get("suggested_health_check_path", "/"),
            "package_scripts": scripts,
            "package_main": main_entry,
            "infra_signals": {
                "needs_database": infra.needs_database,
                "database_hint": infra.database_hint,
                "needs_cache": infra.needs_cache,
                "cache_hint": infra.cache_hint,
                "needs_object_storage": infra.needs_object_storage,
                "storage_hint": infra.storage_hint,
                "is_static_site": infra.is_static_site,
            } if infra else None,
        }
    except Exception as e:
        logger.warning("copilot_github_inspection_skipped", owner=owner, repo=repo, error=str(e))
        return None


@router.post("/chat", response_model=CopilotChatResponse)
async def chat_with_copilot(
    body: CopilotChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_request_db),
):
    """
    Multi-turn conversation with the AI DevOps Copilot.
    Context is stored purely in that chat session (passed via body.messages).
    """
    tenant_id = _get_tenant_id(request)

    project_context = None
    if body.project_id:
        # Securely load project under active tenant RLS
        query = text(
            """
            SELECT id, name, repository_url, git_branch, deploy_target, deploy_mode,
                   status, live_url, path_prefix
            FROM projects
            WHERE id = :project_id AND tenant_id = :tenant_id
            LIMIT 1
            """
        )
        res = await db.execute(query, {"project_id": body.project_id, "tenant_id": tenant_id})
        row = res.mappings().first()
        if row:
            project_context = {
                "id": str(row["id"]),
                "name": row["name"],
                "repository_url": row["repository_url"],
                "git_branch": row["git_branch"],
                "deploy_target": row["deploy_target"],
                "deploy_mode": row["deploy_mode"],
                "status": row["status"],
                "live_url": row["live_url"],
                "path_prefix": row["path_prefix"],
            }

    # Inspect GitHub repository if active in wizard_context or mentioned in query
    wizard_ctx = dict(body.wizard_context or {})
    owner = wizard_ctx.get("owner")
    repo = wizard_ctx.get("repo")
    branch = wizard_ctx.get("branch") or "main"

    if not (owner and repo):
        repo_url = wizard_ctx.get("repo_url", "")
        m = re.search(r"github\.com[/:]([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)", repo_url)
        if m:
            owner, repo = m.group(1), m.group(2).removesuffix(".git")

    if not (owner and repo) and body.messages:
        last_msg = body.messages[-1].content
        m = re.search(r"github\.com[/:]([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)", last_msg)
        if m:
            owner, repo = m.group(1), m.group(2).removesuffix(".git")

    if owner and repo and "github_inspection" not in wizard_ctx:
        inspection = await _inspect_github_repo(request, owner, repo, branch)
        if inspection:
            wizard_ctx["github_inspection"] = inspection

    payload = {
        "messages": [m.model_dump() for m in body.messages],
        "project_context": project_context,
        "wizard_context": wizard_ctx,
    }

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(
                f"{EXPLAINABILITY_SERVICE_URL}/copilot/converse",
                json=payload,
            )
    except httpx.RequestError as e:
        logger.error("copilot_service_unreachable", error=str(e))
        raise HTTPException(status_code=502, detail=f"DevOps Copilot service unavailable: {e}")

    if resp.status_code >= 400:
        logger.error("copilot_service_error", status_code=resp.status_code, body=resp.text)
        raise HTTPException(status_code=502, detail="DevOps Copilot failed to process request")

    data = resp.json()
    # Defense-in-depth output sanitization
    safe_reply = sanitize_secrets(data.get("reply", ""))
    return CopilotChatResponse(
        reply=safe_reply,
        suggested_actions=data.get("suggested_actions", []),
    )
