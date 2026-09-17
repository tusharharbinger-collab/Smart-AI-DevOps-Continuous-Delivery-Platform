"""
services/explainability-service/src/digest_summarizer.py

AI-generated plain-language summary of the periodic delivery-health digest
(Report 3, §12.3, digest_generator.py) — P0, 2026-09-16. "Why did rollback
rate go up this month," grounded ENTIRELY in the real numbers
digest_generator.py already computed from real `pipeline_executions`/
`verification_records`/`cost_analysis` rows — never a second data source,
never a number invented past what those queries returned.

Same Groq call shape as report_generator.py/stage_failure_analyzer.py
(hard timeout, JSON-object response, pydantic validation, deterministic
fallback) — never a second integration pattern. The digest itself has
already been computed and would be returned regardless of what happens
here, so a Groq outage must never block or delay it — it only affects
whether the human-readable summary is AI-written or template-written.
"""
import os

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_INSTRUCTION = (
    "You are the Delivery Health Analyst of an enterprise delivery platform. You are given "
    "a tenant's real deployment digest numbers for a trailing period. Your summary MUST be "
    "grounded ENTIRELY in the numbers provided — do not invent a specific incident or cause "
    "the numbers don't support. Respond with a single JSON object with exactly these keys: "
    "summary (string, two or three sentences in plain language), "
    "notable_trend (string, exactly one of: 'improving', 'stable', 'degrading'), "
    "talking_points (array of strings, at most 3, each one citing a specific number from the input)."
)


class DigestSummary(BaseModel):
    summary: str
    notable_trend: str
    talking_points: list[str]


async def generate_digest_summary(digest: dict, timeout_seconds: float = 30.0) -> dict:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.info("groq_digest_summary_skipped_no_api_key", tenant_id=digest.get("tenant_id"))
        return _fallback_summary(digest)

    prompt = (
        f"Tenant: {digest.get('tenant_id')}\n"
        f"Period: last {digest.get('period_days')} days\n"
        f"Total deployments: {digest.get('total_deployments')}\n"
        f"Rollback count: {digest.get('rollback_count')}\n"
        f"Rollback rate: {digest.get('rollback_rate_percent')}%\n"
        f"Pipeline success rate: {digest.get('pipeline_success_rate_percent')}%\n"
        f"Mean time to verify (seconds): {digest.get('mean_time_to_verify_seconds')}\n"
        f"Average cost delta: {digest.get('avg_cost_delta_percent')}%\n"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": _SYSTEM_INSTRUCTION},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        parsed = DigestSummary.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException:
        logger.warning(
            "groq_digest_summary_timeout", tenant_id=digest.get("tenant_id"), timeout_seconds=timeout_seconds
        )
        return _fallback_summary(digest)
    except Exception as e:
        logger.error("groq_digest_summary_failed", tenant_id=digest.get("tenant_id"), error=str(e))
        return _fallback_summary(digest)


def _fallback_summary(digest: dict) -> dict:
    """Deterministic, template-based fallback — never blocks the digest response."""
    total = digest.get("total_deployments") or 0
    rollback_rate = digest.get("rollback_rate_percent") or 0

    if total == 0:
        trend, summary = "stable", "No deployments recorded in this period."
    elif rollback_rate == 0:
        trend, summary = "improving", f"{total} deployment(s) with zero rollbacks in this period."
    elif rollback_rate < 10:
        trend, summary = "stable", f"{total} deployment(s) with a {rollback_rate}% rollback rate."
    else:
        trend = "degrading"
        summary = f"{total} deployment(s) with an elevated {rollback_rate}% rollback rate — worth investigating."

    return {
        "summary": summary,
        "notable_trend": trend,
        "talking_points": [
            f"{digest.get('pipeline_success_rate_percent', 0)}% pipeline success rate",
            f"{digest.get('rollback_count', 0)} rollback(s) out of {total} deployment(s)",
        ],
    }
