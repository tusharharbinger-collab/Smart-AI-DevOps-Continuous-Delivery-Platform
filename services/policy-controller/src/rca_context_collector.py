"""
services/policy-controller/src/rca_context_collector.py

Collects multimodal context (commit diff, container runtime error logs from
Loki or CloudWatch) to enrich explainability-service's Groq RCA prompt.

Strictly non-invasive & fail-soft:
- Runs AFTER actuation/rollback is complete.
- Every external call is bounded by a 3.0-second timeout.
- Any error (auth failure, rate limit, missing log group, network blip)
  is caught and logged as a warning; an empty result is returned so the
  base statistical RCA continues without interruption.
"""
import asyncio
import os
import re
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


def _parse_github_owner_repo(repo_url: str | None) -> tuple[str, str] | None:
    """Extracts (owner, repo) from a git or https URL."""
    if not repo_url:
        return None
    match = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?$", repo_url.strip())
    if match:
        return match.group(1), match.group(2)
    return None


async def _fetch_github_diff(repo_url: str | None, commit_sha: str | None, timeout_s: float = 3.0) -> str | None:
    """Fetches the commit unified diff via GitHub API."""
    if not repo_url or not commit_sha:
        return None
    parsed = _parse_github_owner_repo(repo_url)
    if not parsed:
        return None
    owner, repo = parsed

    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "User-Agent": "SmartCD-RCA-Collector/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit_sha}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(api_url, headers=headers)
            if resp.status_code == 200:
                diff_text = resp.text
                if len(diff_text) > 4000:
                    diff_text = diff_text[:4000] + "\n... [diff truncated for length]"
                return diff_text
            else:
                logger.debug("github_diff_fetch_non_200", status=resp.status_code, repo=f"{owner}/{repo}")
    except Exception as exc:
        logger.debug("github_diff_fetch_failed", error=str(exc), repo=f"{owner}/{repo}")
    return None


async def _fetch_loki_error_logs(service_name: str | None, timeout_s: float = 3.0) -> list[str]:
    """Queries Loki for recent error / 5xx / exception logs."""
    try:
        now_ns = int(time.time() * 1e9)
        start_ns = now_ns - int(15 * 60 * 1e9)
        query = '{service=~".+"} |= "error" or {service=~".+"} |= "500" or {service=~".+"} |= "Exception"'
        if service_name:
            query = f'{{service=~".*{service_name}.*"}} |= "error" or {{service=~".*{service_name}.*"}} |= "500" or {{service=~".*{service_name}.*"}} |= "Exception"'

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": start_ns,
                    "end": now_ns,
                    "limit": 20,
                    "direction": "BACKWARD",
                },
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                results = data.get("result", [])
                lines = []
                for stream in results:
                    for entry in stream.get("values", []):
                        if len(entry) >= 2:
                            lines.append(entry[1].strip())
                return lines[:15]
    except Exception as exc:
        logger.debug("loki_logs_fetch_failed", error=str(exc))
    return []


async def _fetch_cloudwatch_error_logs(service_name: str | None, timeout_s: float = 3.0) -> list[str]:
    """Queries CloudWatch Logs for recent container errors if running on AWS ECS."""
    if not service_name:
        return []

    def _sync_fetch() -> list[str]:
        import boto3
        log_group_candidates = [
            f"/ecs/smartcd-platform/{service_name}-canary",
            f"/ecs/smartcd-platform/{service_name}",
        ]
        logs_client = boto3.client("logs", region_name=AWS_REGION)
        for log_group in log_group_candidates:
            try:
                start_time_ms = int((time.time() - 900) * 1000)
                resp = logs_client.filter_log_events(
                    logGroupName=log_group,
                    filterPattern="?ERROR ?500 ?Exception ?Traceback ?fail",
                    startTime=start_time_ms,
                    limit=20,
                )
                events = resp.get("events", [])
                if events:
                    return [e.get("message", "").strip() for e in events if e.get("message")]
            except Exception:
                continue
        return []

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_fetch), timeout=timeout_s)
    except Exception as exc:
        logger.debug("cloudwatch_logs_fetch_failed", error=str(exc))
        return []


async def gather_rca_context(db: Any, tenant_id: str | None, pipeline_run_id: str | None) -> dict:
    """
    Safely collects commit diff and runtime container logs.
    Guaranteed never to raise: returns safe empty structures on any failure.
    """
    context: dict[str, Any] = {
        "service_name": None,
        "commit_sha": None,
        "commit_message": None,
        "commit_diff": None,
        "error_logs": [],
    }

    if not db or not tenant_id or not pipeline_run_id:
        return context

    try:
        exec_ctx = await db.get_execution_context_for_rca(tenant_id, pipeline_run_id)
        if not exec_ctx:
            return context

        service_name = exec_ctx.get("service_name") or exec_ctx.get("project_name")
        commit_sha = exec_ctx.get("commit_sha")
        repo_url = exec_ctx.get("repo_url")
        deploy_target = exec_ctx.get("deploy_target") or "kubernetes"

        context["service_name"] = service_name
        context["commit_sha"] = commit_sha
        context["commit_message"] = exec_ctx.get("commit_message")

        fetch_logs_coro = (
            _fetch_cloudwatch_error_logs(service_name)
            if deploy_target == "aws_ecs"
            else _fetch_loki_error_logs(service_name)
        )

        diff_result, logs_result = await asyncio.gather(
            _fetch_github_diff(repo_url, commit_sha),
            fetch_logs_coro,
            return_exceptions=True,
        )

        if isinstance(diff_result, str):
            context["commit_diff"] = diff_result
        if isinstance(logs_result, list):
            context["error_logs"] = logs_result

    except Exception as exc:
        logger.warning("rca_context_collection_failed_softly", error=str(exc), pipeline_run_id=pipeline_run_id)

    return context
