"""
services/explainability-service/src/stage_failure_analyzer.py

Grounded RCA for a failed pipeline STAGE (build/test/deploy) — the gap
found comparing this platform against Harness's "Software Delivery Agent":
report_generator.py's generate_rca() only ever explains a verification
verdict; a failed build/test stage previously just logged the raw
exception with no explanation. Same Groq call shape as generate_rca()
(hard timeout, JSON-object response, pydantic validation) and the same
non-negotiable fallback guarantee: the pipeline has ALREADY been marked
FAILED by pipeline-worker by the time this runs, so a Groq outage must
never block or delay that — it only affects the quality of the
after-the-fact explanation.
"""
import json
import os

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_INSTRUCTION = (
    "You are the Pipeline Failure Analyst of an enterprise delivery platform. A pipeline "
    "stage failed. Your explanation MUST be grounded ENTIRELY in the error message and log "
    "lines provided — do not invent a cause the evidence doesn't support. "
    "Respond with a single JSON object with exactly these keys: "
    "likely_cause (string, one or two sentences), "
    "evidence (array of strings, the exact log lines or error text that support the cause), "
    "suggested_fix (string)."
)


class StageFailureRCA(BaseModel):
    likely_cause: str
    evidence: list[str]
    suggested_fix: str


async def generate_stage_failure_rca(
    run_id: str,
    failed_stage: str,
    error_message: str,
    recent_logs: list[str],
    timeout_seconds: float = 30.0,
) -> dict:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.info("groq_stage_failure_rca_skipped_no_api_key", run_id=run_id)
        return _fallback_rca(failed_stage, error_message)

    # Last 50 lines is enough context for a stage failure without risking the
    # free-tier per-request token limit on a long build/test log.
    log_tail = recent_logs[-50:]
    prompt = (
        f"Stage '{failed_stage}' failed for pipeline run {run_id}.\n"
        f"Error: {error_message}\n\n"
        f"Recent log lines:\n" + "\n".join(log_tail)
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

        parsed = StageFailureRCA.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException:
        logger.warning("groq_stage_failure_rca_timeout", run_id=run_id, timeout_seconds=timeout_seconds)
        return _fallback_rca(failed_stage, error_message)
    except Exception as e:
        logger.error("groq_stage_failure_rca_failed", run_id=run_id, error=str(e))
        return _fallback_rca(failed_stage, error_message)


def _fallback_rca(failed_stage: str, error_message: str) -> dict:
    """Deterministic, template-based fallback — never blocks failure recording."""
    return {
        "likely_cause": f"Stage '{failed_stage}' failed: {error_message} (Groq RCA unavailable; showing raw error).",
        "evidence": [error_message],
        "suggested_fix": "Review the full stage log in the Pipeline View for more detail.",
    }
