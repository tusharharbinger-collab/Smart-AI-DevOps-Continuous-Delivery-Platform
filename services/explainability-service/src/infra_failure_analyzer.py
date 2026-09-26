"""
services/explainability-service/src/infra_failure_analyzer.py

Backlog #4 - grounded root-cause analysis for a FAILED infrastructure change (a CloudFormation change set that
could not be created, or a stack that failed/rolled back while provisioning).

Three properties that matter more than the prose:

1. Root cause selection is deterministic. A failing CloudFormation stack emits many FAILED events, but almost all
   are fallout ("Resource creation cancelled") from the ONE real failure. `select_root_cause_events` keeps the
   earliest event with a real reason, so the model is asked about the cause, not the noise.
2. Evidence is verified in code. The model must quote the provided events/reasons; any "evidence" line that is not
   actually present in the input is dropped (a fabricated log line is worse than none).
3. It always answers. A deterministic classifier covers the common failure families, so a Groq outage, rate limit
   or bad output still yields a useful, honest explanation - the analysis is advice after the fact, never a gate.

Also returns `suggested_edit_instruction` when the fix is a template change, so the UI can hand it straight to the
"Edit with AI" box (which re-enters the normal approval gate - nothing is applied automatically).
"""
import asyncio
import json
import os
import re

import httpx
import structlog
from pydantic import BaseModel, field_validator

logger = structlog.get_logger(__name__)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

CATEGORIES = ("name_conflict", "permissions", "quota", "invalid_configuration", "dependency", "unknown")

# Reasons that describe fallout from another resource failing, not a cause of their own.
_CASCADE_RE = re.compile(r"(resource (creation|update|deletion) cancelled|cancelled|rollback requested|"
                         r"the following resource\(s\) failed)", re.IGNORECASE)
_FAILED_STATUSES = ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "ROLLBACK_FAILED", "UPDATE_ROLLBACK_FAILED")

# (category, pattern, cause, fix, edit instruction or None) - checked in order; the first match wins.
_RULES: list[tuple[str, re.Pattern, str, str, str | None]] = [
    ("invalid_configuration", re.compile(r"Template format error|Unresolved resource dependencies|Template error|"
                                        r"Circular dependency|Unresolved condition", re.I),
     "The CloudFormation template itself is malformed - a reference points at something that is not defined in it.",
     "Define the missing resource or correct the reference named in the error, then preview again.",
     "Fix the template error reported: define the missing resource or correct the reference it names"),
    ("name_conflict", re.compile(r"already exists|AlreadyExists|is in use|already in use|duplicate", re.I),
     "A resource with this name already exists in the account, and CloudFormation cannot create a second one.",
     "Give the resource a unique name (or remove the explicit name so AWS generates one), or attach the existing "
     "resource instead of creating it.",
     "Remove the hard-coded name from the failing resource so AWS generates a unique one"),
    ("permissions", re.compile(r"AccessDenied|not authorized|UnauthorizedOperation|is not authorized to perform|"
                               r"Access Denied|forbidden", re.I),
     "The role used for provisioning is not allowed to perform this action.",
     "Grant the missing permission to the provisioning role (for a connected AWS account, update the "
     "'smartcd-platform-access' stack), then retry.", None),
    ("quota", re.compile(r"LimitExceeded|limit exceeded|quota|maximum number of|exceeds the (maximum|limit)|"
                         r"TooManyEntities|InsufficientCapacity", re.I),
     "An AWS service limit or capacity was hit while creating this resource.",
     "Request a quota increase, delete unused resources of this type, or use a smaller/different configuration.", None),
    ("invalid_configuration", re.compile(r"InvalidParameter|ValidationError|Invalid |not supported|unsupported|"
                                        r"Property .* cannot be empty|Encountered unsupported property|"
                                        r"is not a valid|must be", re.I),
     "A property of this resource has a value AWS rejects.",
     "Correct the invalid property named in the error and retry.",
     "Fix the invalid configuration reported by the failing resource"),
    ("dependency", re.compile(r"subnet|vpc|security group|does not exist|not found|DependencyViolation|"
                              r"has a dependent|cannot be deleted|in a different", re.I),
     "The resource depends on another AWS resource (network, security group, subnet or role) that is missing, "
     "in a different VPC, or still in use.",
     "Check that every referenced network resource exists in the same VPC and region, then retry.", None),
]


class InfraFailureRCA(BaseModel):
    likely_cause: str
    evidence: list[str]
    suggested_fix: str
    suggested_edit_instruction: str | None = None
    category: str = "unknown"

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        return v if v in CATEGORIES else "unknown"


def select_root_cause_events(events: list[dict]) -> list[dict]:
    """
    Earliest-first failed events with a REAL reason. Falls back to all failed events if every one looks like
    cascade fallout (still better than nothing), and to [] when there are no failed events.
    """
    failed = sorted(
        (e for e in events if e.get("status") in _FAILED_STATUSES and e.get("reason")),
        key=lambda e: e.get("timestamp") or "",
    )
    real = [e for e in failed if not _CASCADE_RE.search(e["reason"])]
    return real or failed


def _event_line(e: dict) -> str:
    return f'{e.get("resource")} ({e.get("type")}) {e.get("status")}: {e.get("reason")}'


def _evidence_pool(status_reason: str | None, events: list[dict]) -> list[str]:
    pool = [_event_line(e) for e in events]
    if status_reason:
        pool.append(status_reason)
    return pool


def classify(text: str) -> tuple[str, str, str, str | None] | None:
    for category, pattern, cause, fix, edit in _RULES:
        if pattern.search(text):
            return category, cause, fix, edit
    return None


def deterministic_rca(phase: str, status_reason: str | None, events: list[dict]) -> dict:
    """Always available, always grounded: built only from the real events/reasons and the rule table above."""
    roots = select_root_cause_events(events)
    pool = _evidence_pool(status_reason, roots[:3])
    text = " ".join(pool)
    hit = classify(text)
    subject = f"'{roots[0]['resource']}' ({roots[0]['type']})" if roots else "the change"
    if hit:
        category, cause, fix, edit = hit
        return {
            "likely_cause": f"{subject} failed: {cause}",
            "evidence": pool[:5],
            "suggested_fix": fix,
            "suggested_edit_instruction": edit,
            "category": category,
        }
    where = "creating the change set" if phase == "change_set" else "provisioning the stack"
    return {
        "likely_cause": f"AWS reported a failure while {where}" + (f" on {subject}." if roots else "."),
        "evidence": pool[:5] or ["(AWS returned no failure detail)"],
        "suggested_fix": "Read the evidence above for the exact AWS error, correct the resource it names, and retry.",
        "suggested_edit_instruction": None,
        "category": "unknown",
    }


def ground_evidence(evidence: list[str], pool: list[str]) -> list[str]:
    """Keeps only evidence lines that really appear in the provided events/reasons (verbatim substring)."""
    haystack = "\n".join(pool)
    return [e for e in evidence if isinstance(e, str) and e.strip() and e.strip() in haystack]


_SYSTEM_INSTRUCTION = (
    "You are the Infrastructure Failure Analyst of a continuous-delivery platform. An AWS CloudFormation change "
    "failed. Explain the ROOT CAUSE using ONLY the events and reasons provided - never invent a cause or quote "
    "text that is not in them. The events are already filtered to the root failures (cancelled-resource fallout "
    "is removed). Respond with a single JSON object with exactly these keys: likely_cause (one or two sentences), "
    "evidence (array of strings - each MUST be copied verbatim from the provided events/reasons), suggested_fix "
    "(string), suggested_edit_instruction (a short imperative instruction that could be given to an infrastructure "
    "editing assistant to fix the template, or null when the fix is not a template change - e.g. a permission or "
    "quota problem), category (one of: " + ", ".join(CATEGORIES) + ")."
)


async def analyze_infra_failure(
    draft_id: str, phase: str, status_reason: str | None, events: list[dict],
    resources: list[dict] | None = None, timeout_seconds: float = 30.0,
) -> dict:
    roots = select_root_cause_events(events)
    pool = _evidence_pool(status_reason, roots[:5])
    fallback = deterministic_rca(phase, status_reason, events)

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return {**fallback, "source": "deterministic"}

    prompt = (
        f"Phase: {'change set creation' if phase == 'change_set' else 'stack provisioning'}\n"
        f"Stack/change-set status reason: {status_reason or '(none)'}\n"
        "Root failure events (earliest first):\n" + ("\n".join(_event_line(e) for e in roots[:5]) or "(none)") + "\n"
        "Template resources: " + json.dumps(resources or [])[:1500]
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": GROQ_MODEL, "temperature": 0.0, "max_tokens": 2048,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": _SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}],
                },
            )
        response.raise_for_status()
        parsed = InfraFailureRCA.model_validate_json(response.json()["choices"][0]["message"]["content"])
    except httpx.TimeoutException:
        logger.warning("infra_failure_rca_timeout", draft_id=draft_id)
        return {**fallback, "source": "deterministic"}
    except Exception as e:  # rate limit, bad JSON, validation - the analysis must never fail the caller
        logger.warning("infra_failure_rca_model_failed", draft_id=draft_id, error=str(e) or type(e).__name__)
        return {**fallback, "source": "deterministic"}

    grounded = ground_evidence(parsed.evidence, pool)
    if not grounded:
        # Nothing the model quoted is real: do not present its story as grounded. Use the verified fallback evidence.
        logger.warning("infra_failure_rca_ungrounded_evidence_discarded", draft_id=draft_id)
        return {**fallback, "likely_cause": parsed.likely_cause or fallback["likely_cause"], "source": "model_ungrounded"}
    return {
        "likely_cause": parsed.likely_cause,
        "evidence": grounded,
        "suggested_fix": parsed.suggested_fix,
        "suggested_edit_instruction": parsed.suggested_edit_instruction or None,
        "category": parsed.category,
        "source": "model",
    }
