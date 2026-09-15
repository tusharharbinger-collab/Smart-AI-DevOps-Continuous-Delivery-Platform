"""
shared/opa_client.py

Thin, synchronous OPA REST client for services other than policy-controller
that occasionally need a real guardrail decision without going through the
full verdict-signing/publish/consume round trip.

First real caller: pipeline-worker's first-deployment path (worker.py) — a
project's genuinely first-ever deployment has no statistical verdict to
gate (see policies/delivery_guardrails.rego's Rule 9, "FIRST_DEPLOYMENT"),
but the freeze-window guardrail still meaningfully applies even with zero
evidence. This intentionally does NOT duplicate the freeze-window
day/time-window comparison logic in Python — that stays defined exactly
once, in Rego — it only duplicates the thin HTTP call shape that
policy-controller's own opa_evaluator.py already makes, so pipeline-worker
doesn't need a dependency on policy-controller's own service code (a
different container/deployable).
"""
import os

import httpx
import structlog

logger = structlog.get_logger(__name__)

OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")
OPA_POLICY_PATH = "/v1/data/delivery/guardrails"


def evaluate_policy_sync(payload: dict) -> dict:
    """
    Returns {"allow_action": bool, "rejection_reasons": list[str], "opa_unreachable": bool}.
    Fails closed (allow_action=False) on any error — a first deployment
    that can't reach OPA to check the freeze window should wait, not guess.
    """
    url = f"{OPA_URL}{OPA_POLICY_PATH}"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json={"input": payload})
            resp.raise_for_status()
            result = resp.json().get("result", {})
            return {
                "allow_action": result.get("allow_action", False),
                "rejection_reasons": result.get("rejection_reasons", []),
                "opa_unreachable": False,
            }
    except Exception as e:
        logger.error("opa_evaluation_failed", error=str(e), url=url)
        return {
            "allow_action": False,
            "rejection_reasons": [f"OPA evaluation error: {e}"],
            "opa_unreachable": True,
        }
