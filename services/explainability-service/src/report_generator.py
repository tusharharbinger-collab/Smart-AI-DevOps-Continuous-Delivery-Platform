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
    "Your summary MUST be grounded ENTIRELY in the numerical facts, container error logs, and git commit diff provided. "
    "Do not infer causes not supported by the data. "
    "Cite exact metrics, deltas, and statistical test values verbatim from the input. "
    "When runtime container error logs and git commit diffs are present, analyze them to locate the bug in code "
    "and synthesize a precise code fix in standard unified diff format (--- a/... +++ b/...). "
    "Respond with a single JSON object with exactly these keys: "
    "executive_summary (string, two sentences explaining why the verdict fired), "
    "root_cause_file (string or null, file path where the regression was introduced), "
    "line_number (integer or null, approximate line number in root_cause_file), "
    "suspect_commit (string or null, the commit sha or message that introduced the bug), "
    "error_log_snippet (string or null, key runtime error log line or traceback), "
    "suggested_patch (string or null, standard git unified diff fixing the bug: --- a/... +++ b/...), "
    "suggested_remediation (string, concise actionable remediation advice), "
    "triggering_metrics (array of {metric_name, citation}), "
    "policy_clauses_evaluated (array of strings)."
)


class MetricEvidence(BaseModel):
    metric_name: str
    citation: str


class RCAReport(BaseModel):
    executive_summary: str = Field(description="Two-sentence summary of WHY the decision fired")
    root_cause_file: str | None = None
    line_number: int | None = None
    suspect_commit: str | None = None
    error_log_snippet: str | None = None
    suggested_patch: str | None = None
    suggested_remediation: str
    triggering_metrics: list[MetricEvidence] = Field(default_factory=list)
    policy_clauses_evaluated: list[str] = Field(default_factory=list)


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


_DEGRADATION_SYSTEM_INSTRUCTION = (
    "You are the SRE Verification Diagnostician of an enterprise delivery platform. "
    "A canary deployment has received a DEGRADED status (statistical drift, low sample velocity, or latency/error warnings). "
    "Analyze the provided verification metrics, runtime container logs, and commit diff. "
    "Explain in clear, actionable terms WHY the release is degrading, what ways exist to stabilize or improve it, "
    "and what specific code or infrastructure changes are required to pass verification. "
    "If a code issue is evident, generate a precise unified git diff (--- a/... +++ b/...). "
    "Respond ONLY with a single JSON object with these keys: "
    "executive_summary (string, two sentences explaining why it is degrading), "
    "degradation_reason (string, primary factor: e.g. 'Latency P95 Drift', 'Accumulating Error Proportion', 'Sample Velocity Below Floor'), "
    "root_cause_file (string or null), "
    "line_number (integer or null), "
    "suggested_patch (string or null, unified diff: --- a/... +++ b/...), "
    "ways_to_improve (array of strings, concrete ways to make it better and pass verification), "
    "required_changes (string, concise actionable summary of changes needed)."
)


class DegradationReport(BaseModel):
    executive_summary: str
    degradation_reason: str
    root_cause_file: str | None = None
    line_number: int | None = None
    suggested_patch: str | None = None
    ways_to_improve: list[str] = Field(default_factory=list)
    required_changes: str


async def generate_degradation_diagnosis(analysis_data: dict, timeout_seconds: float = 30.0) -> dict:
    """
    Analyzes why a canary verdict is DEGRADED and prescribes ways to improve and pass verification.
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.info("degradation_diagnosis_skipped_no_api_key")
        return _fallback_degradation_diagnosis(analysis_data)

    prompt = f"Analyze this degraded verification state and produce diagnostic recommendations:\n{json.dumps(analysis_data, indent=2, default=str)}"

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
                        {"role": "system", "content": _DEGRADATION_SYSTEM_INSTRUCTION},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        parsed = DegradationReport.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException:
        logger.warning("groq_degradation_timeout", timeout_seconds=timeout_seconds)
        return _fallback_degradation_diagnosis(analysis_data)
    except Exception as e:
        logger.error("groq_degradation_failed", error=str(e))
        return _fallback_degradation_diagnosis(analysis_data)


def _fallback_degradation_diagnosis(analysis_data: dict) -> dict:
    """Deterministic fallback for degradation diagnosis."""
    confidence = analysis_data.get("confidence", 0.0)
    sample_size = analysis_data.get("sample_size") or analysis_data.get("sample_count", 0)
    service_name = analysis_data.get("service_name") or analysis_data.get("service", "the service")

    ways = [
        "Increase synthetic traffic or allow more production requests to reach the N >= 100 sample floor.",
        "Inspect container CloudWatch logs for intermittent slow database queries or connection retry loops.",
        "Verify that CPU and memory utilization on the canary Fargate task remain below 80% saturation."
    ]

    reason = "Sample Size Under Floor (N < 100)" if (sample_size and sample_size < 100) else "Statistical Variance / Telemetry Accumulating"

    return {
        "executive_summary": (
            f"Canary for {service_name} is in DEGRADED status (confidence: {confidence:.2f}). "
            "Telemetry is accumulating or showing minor variance drift before the confidence floor is reached."
        ),
        "degradation_reason": reason,
        "root_cause_file": None,
        "line_number": None,
        "suggested_patch": None,
        "ways_to_improve": ways,
        "required_changes": "Allow the canary dwell time to elapse to accumulate sufficient traffic samples.",
    }


def _fallback_rca(analysis_data: dict) -> dict:
    """Deterministic, template-based fallback — never blocks the decision report."""
    raw_logs = analysis_data.get("error_logs") or []
    err_snippet = raw_logs[0] if (isinstance(raw_logs, list) and len(raw_logs) > 0) else None
    service_name = analysis_data.get("service_name") or analysis_data.get("service", "the service")
    return {
        "executive_summary": (
            f"Verdict {analysis_data.get('final_verdict')} reached for "
            f"{service_name} "
            "(Groq RCA unavailable; showing raw evidence)."
        ),
        "root_cause_file": None,
        "line_number": None,
        "suspect_commit": analysis_data.get("commit_sha"),
        "error_log_snippet": err_snippet,
        "suggested_patch": None,
        "triggering_metrics": analysis_data.get("metric_evidence", []),
        "policy_clauses_evaluated": analysis_data.get("policy_checks", []),
        "suggested_remediation": "Review raw metric evidence in the Verification Inspector.",
    }

