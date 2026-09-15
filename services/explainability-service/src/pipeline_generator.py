"""
services/explainability-service/src/pipeline_generator.py

Natural-language pipeline authoring — the AI proposes a modified pipeline
YAML from a free-form request, but never has a path to commit it: the
caller (api-gateway) always runs the result through pipeline-worker's real
validator (the exact same safety-critical rules a hand-authored pipeline
must pass, see schemas.py) before it can ever reach a human's screen for
review, and even then it's populated into the existing YAML editor for the
user to explicitly Save — never auto-applied. Mirrors report_generator.py's
Groq call shape (hard timeout, JSON-object response format, pydantic
validation) with one difference: there is no safety action riding on this
call the way there is on generate_rca(), so on failure this raises instead
of silently falling back to a fabricated "explanation" of a policy change
nobody actually reviewed.
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
    "You are a pipeline configuration assistant for an enterprise continuous-delivery "
    "platform. You are given an existing pipeline's YAML and a natural-language request "
    "describing a change. Produce a MODIFIED version of the full YAML that satisfies the "
    "request, changing only what the request actually asks for and leaving everything else "
    "byte-for-byte identical. The YAML must keep the exact same top-level shape "
    "(apiVersion/kind/metadata/spec with stages/gates/guardrails/verificationConfig). "
    "Canary traffic steps must be strictly increasing and end at 100. Every automated step "
    "(requiresManualApproval not true) must have a positive minDuration and minSampleSize. "
    "requireMinimumConfidence must be in (0.0, 1.0]. "
    "Respond with a single JSON object with exactly these keys: "
    "pipeline_yaml (string, the complete modified YAML document), "
    "summary_of_changes (string, one or two sentences describing exactly what changed)."
)


class PipelineGenerationError(Exception):
    """Raised when Groq is unavailable or returns an unusable response — no safety action
    depends on this call, so callers should surface a clear error rather than fabricate
    a policy change nobody reviewed."""


class PipelineGenerationResult(BaseModel):
    pipeline_yaml: str
    summary_of_changes: str


async def generate_pipeline_yaml(
    prompt: str,
    current_yaml: str,
    context: dict,
    validation_error: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict:
    """
    `validation_error`, when set, is the real error pipeline-worker's
    /pipelines/validate returned for a PRIOR attempt at this same request —
    fed back verbatim so the model sees exactly what it got wrong, matching
    how a human would be shown the same validator error and asked to fix it.
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise PipelineGenerationError("GROQ_API_KEY is not configured — AI pipeline authoring is unavailable.")

    user_content = (
        f"Current pipeline YAML:\n{current_yaml}\n\n"
        f"Context: {json.dumps(context, default=str)}\n\n"
        f"Requested change: {prompt}"
    )
    if validation_error:
        user_content += (
            f"\n\nYour previous attempt was rejected by the pipeline validator with this "
            f"exact error — fix it: {validation_error}"
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
                        {"role": "user", "content": user_content},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

        parsed = PipelineGenerationResult.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException as e:
        logger.warning("groq_pipeline_generation_timeout", timeout_seconds=timeout_seconds)
        raise PipelineGenerationError(f"AI pipeline generation timed out after {timeout_seconds}s.") from e
    except Exception as e:
        logger.error("groq_pipeline_generation_failed", error=str(e))
        raise PipelineGenerationError(f"AI pipeline generation failed: {e}") from e
