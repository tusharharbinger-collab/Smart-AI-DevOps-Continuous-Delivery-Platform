"""
services/explainability-service/src/report_generator.py

Groq-backed RCA integration with a hard 30-second timeout. Spec §9.2 (originally
written against Gemini 2.5 Flash; swapped to Groq's OpenAI-compatible chat
completions API — same JSON-grounded contract, same fallback guarantee).

On timeout or any API failure, generate_rca() falls back to a deterministic,
template-based summary. This is essential: the rollback/promotion decision
has ALREADY been executed by policy-controller by the time this function
runs, so a Groq outage must never block or delay the actual safety action —
it only affects the quality of the after-the-fact human-readable explanation.
"""
import json
import os

import httpx
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_INSTRUCTION = (
    "You are the Verification Reasoning Engine of an enterprise delivery platform. "
    "Your summary MUST be grounded ENTIRELY in the numerical facts and citation "
    "strings provided. Do not infer causes not supported by the data. "
    "Cite exact metrics, deltas, and statistical test values verbatim from the input. "
    "Respond with a single JSON object with exactly these keys: "
    "executive_summary (string, two sentences), "
    "triggering_metrics (array of {metric_name, citation}), "
    "policy_clauses_evaluated (array of strings), "
    "suggested_remediation (string)."
)


class MetricEvidence(BaseModel):
    metric_name: str
    citation: str


class RCAReport(BaseModel):
    executive_summary: str = Field(description="Two-sentence summary of WHY the decision fired")
    triggering_metrics: list[MetricEvidence]
    policy_clauses_evaluated: list[str]
    suggested_remediation: str


async def generate_rca(analysis_data: dict, timeout_seconds: float = 30.0) -> dict:
    """
    Calls Groq (OpenAI-compatible /chat/completions) with a hard 30-second
    timeout (assignment requirement). GROQ_API_KEY must be set; otherwise
    falls back immediately without attempting a network call.
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.info("groq_rca_skipped_no_api_key")
        return _fallback_rca(analysis_data)

    prompt = f"Analyze this verification result and produce an RCA:\n{json.dumps(analysis_data, indent=2, default=str)}"

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

        parsed = RCAReport.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException:
        logger.warning("groq_rca_timeout", timeout_seconds=timeout_seconds)
        return _fallback_rca(analysis_data)
    except Exception as e:
        logger.error("groq_rca_failed", error=str(e))
        return _fallback_rca(analysis_data)


def _fallback_rca(analysis_data: dict) -> dict:
    """Deterministic, template-based fallback — never blocks the decision report."""
    return {
        "executive_summary": (
            f"Verdict {analysis_data.get('final_verdict')} reached for "
            f"{analysis_data.get('service', 'the service')} "
            "(Groq RCA unavailable; showing raw evidence)."
        ),
        "triggering_metrics": analysis_data.get("metric_evidence", []),
        "policy_clauses_evaluated": analysis_data.get("policy_checks", []),
        "suggested_remediation": "Review raw metric evidence in the Verification Inspector.",
    }
