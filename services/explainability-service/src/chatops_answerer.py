"""
services/explainability-service/src/chatops_answerer.py

ChatOps query interface ("why did the last prod deploy roll back") — the
assignment's own named bonus item: "a query interface that still grounds
its answer in the real comparison data." Same Groq call shape as
report_generator.py/digest_summarizer.py (hard timeout, JSON-object
response, pydantic validation, deterministic fallback) — never a third
integration pattern. This module only ever reads data `chatops_context.py`
already assembled from real tables; it has no path to influence a real
promote/rollback decision, so it can't touch the guardrail boundary those
decisions are judged on.

The one difference from the other two Groq integrations: those summarize
data that's already complete and correct. Here, the user's question might
not be answerable from the runs handed in (e.g. "what happened last month"
when only the last 5 runs were fetched) — the system instruction requires
saying so explicitly rather than guessing, and `confidence` reflects that.
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
    "You are the Delivery ChatOps Assistant of an enterprise CI/CD platform. You are given "
    "a project's recent real deployment runs (status, verdicts, confidence, audit actions, "
    "cost) as JSON, and a human's question about them. Your answer MUST be grounded ENTIRELY "
    "in the run data provided — cite the real run id (first 8 characters are enough) and the "
    "real action/metric/value you are referencing. If the data provided does not contain "
    "enough information to answer the question, say so explicitly rather than guessing. "
    "Respond with a single JSON object with exactly these keys: "
    "answer (string, plain language, 2-4 sentences), "
    "cited_run_ids (array of strings, the pipeline_run_id values your answer actually cites), "
    "confidence (string, exactly one of: 'grounded', 'insufficient_data')."
)


class ChatOpsAnswer(BaseModel):
    answer: str
    cited_run_ids: list[str]
    confidence: str


async def answer_chatops_question(context: dict, question: str, timeout_seconds: float = 30.0) -> dict:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.info("groq_chatops_skipped_no_api_key", project_id=context.get("project_id"))
        return _fallback_answer(context)

    prompt = (
        f"Question: {question}\n\n"
        f"Real run data (most recent first):\n{json.dumps(context, indent=2, default=str)}"
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

        parsed = ChatOpsAnswer.model_validate_json(content)
        return parsed.model_dump()
    except httpx.TimeoutException:
        logger.warning("groq_chatops_timeout", project_id=context.get("project_id"), timeout_seconds=timeout_seconds)
        return _fallback_answer(context)
    except Exception as e:
        logger.error("groq_chatops_failed", project_id=context.get("project_id"), error=str(e))
        return _fallback_answer(context)


def _fallback_answer(context: dict) -> dict:
    """Deterministic, never-fabricated fallback — used both when Groq is
    unconfigured/unreachable AND when there's genuinely no run history to
    answer from. Never invents a cause; points at the real UI instead."""
    runs = context.get("runs") or []
    if not runs:
        return {
            "answer": "This project has no recorded runs yet, so there's nothing to answer from.",
            "cited_run_ids": [],
            "confidence": "insufficient_data",
        }
    return {
        "answer": (
            "The AI assistant is temporarily unavailable. See the Audit Ledger and "
            "Verification Inspector tabs for the same underlying run data."
        ),
        "cited_run_ids": [],
        "confidence": "insufficient_data",
    }
