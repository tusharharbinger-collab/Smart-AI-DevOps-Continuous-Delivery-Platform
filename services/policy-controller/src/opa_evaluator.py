"""
services/policy-controller/src/opa_evaluator.py

Evaluates OPA delivery guardrails via OPA REST API.
"""
import math
import os
import httpx
import structlog

logger = structlog.get_logger(__name__)

OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")
OPA_POLICY_PATH = "/v1/data/delivery/guardrails"


def _json_safe(obj):
    """
    Real bug found live: a verdict's evidence can legitimately contain a
    non-finite float — e.g. Fisher's exact test's odds-ratio `statistic` is
    mathematically infinite whenever one contingency-table cell is exactly
    0 (a real, valid statistical result, not an error). Python's `json.dumps`
    happily emits the literal tokens `Infinity`/`NaN` for these — valid
    Python, but NOT valid JSON per spec — and OPA's strict Go JSON decoder
    rejects the whole request with a 400 the instant one appears anywhere
    in the payload, silently blocking that verdict from ever reaching OPA
    at all (misreported as "OPA unreachable" upstream, since any exception
    here is currently treated that way). Recursively replaces non-finite
    floats with `None` before sending — the Rego policy doesn't read this
    field, so nothing is lost by not shipping it as a number.
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


async def evaluate_policy_async(payload: dict) -> dict:
    """
    Calls OPA REST API to evaluate the delivery guardrails.
    Returns dict with keys:
      allow_action: bool
      require_human_approval: bool
      rejection_reasons: list[str]
      raw_result: dict
      opa_unreachable: bool — True only when OPA itself could not be
        reached/evaluated (network error, timeout, non-2xx) — distinct from
        OPA being reached and legitimately answering "no". Phase 5
        (§05-reliability-scale.md, deliverable 5.4): controller.py needs
        this distinction to apply the right fail-safe default per action
        type — see that module's docstring on `handle_incoming_verdict` for
        why "fail closed" is NOT uniformly the safe default (blocking an
        auto-ROLLBACK is not "safe," it's actively harmful).
    """
    url = f"{OPA_URL}{OPA_POLICY_PATH}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(url, json={"input": _json_safe(payload)})
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", {})
            return {
                "allow_action": result.get("allow_action", False),
                "require_human_approval": result.get("require_human_approval", False),
                "rejection_reasons": result.get("rejection_reasons", []),
                "raw_result": result,
                "opa_unreachable": False,
            }
        except Exception as e:
            logger.error("opa_evaluation_failed", error=str(e), url=url)
            return {
                "allow_action": False,
                "require_human_approval": False,
                "rejection_reasons": [f"OPA evaluation error: {str(e)}"],
                "raw_result": {},
                "opa_unreachable": True,
            }


def evaluate_policy(payload: dict) -> dict:
    """
    Synchronous wrapper around evaluate_policy_async for Celery or sync workers.
    """
    url = f"{OPA_URL}{OPA_POLICY_PATH}"
    with httpx.Client(timeout=5.0) as client:
        try:
            resp = client.post(url, json={"input": _json_safe(payload)})
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", {})
            return {
                "allow_action": result.get("allow_action", False),
                "require_human_approval": result.get("require_human_approval", False),
                "rejection_reasons": result.get("rejection_reasons", []),
                "raw_result": result,
            }
        except Exception as e:
            logger.error("opa_evaluation_failed_sync", error=str(e), url=url)
            return {
                "allow_action": False,
                "require_human_approval": False,
                "rejection_reasons": [f"OPA evaluation error: {str(e)}"],
                "raw_result": {},
            }
