"""
services/policy-controller/tests/test_opa_failsafe.py

Phase 5 (§05-reliability-scale.md, deliverable 5.4): "OPA unreachable ->
fail closed" is safe for PROMOTE_STEP but actively unsafe for ROLLBACK — a
FAILED verdict whose rollback gets blocked just because the guardrail
check itself is down means a known-bad canary keeps serving real traffic.
These tests verify `handle_incoming_verdict` (controller.py) applies the
right default per action type, and — critically — that a REAL policy
rejection (OPA reached, answered "no" for a real reason) is never
overridden, only a genuine "OPA could not be reached" case.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# `src/controller.py` imports `shared.redis_streams` — inside a container
# both `src/` and `shared/` sit under the same /app WORKDIR (see Dockerfile
# + docker-compose's `./shared:/app/shared:ro` mount); from the host, the
# repo root (two levels above services/policy-controller) is where `shared/`
# actually lives.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from src import controller
from src.verdict_verifier import get_signing_key


def _signed_payload(status: str) -> str:
    verdict = {
        "verdict_id": f"v-{status.lower()}",
        "pipeline_run_id": "run-failsafe-test",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "composite_score": 20.0 if status == "FAILED" else 95.0,
        "confidence": 0.9,
    }
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(get_signing_key(), canonical.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"verdict": canonical, "signature": sig})


@pytest.fixture(autouse=True)
def _patch_actuation(monkeypatch):
    """Never let this test suite touch a real Kubernetes API."""
    calls = {"rollback": [], "promote": []}

    async def fake_rollback(pipeline_run_id, authorized_by, **kwargs):
        calls["rollback"].append({"pipeline_run_id": pipeline_run_id, "authorized_by": authorized_by, **kwargs})

    async def fake_promote(pipeline_run_id, canary_weight, baseline_weight, authorized_by, **kwargs):
        calls["promote"].append({"pipeline_run_id": pipeline_run_id, "authorized_by": authorized_by})

    async def fake_send_alert(*args, **kwargs):
        pass

    async def fake_alert_rollback(*args, **kwargs):
        pass

    monkeypatch.setattr(controller, "emergency_rollback", fake_rollback)
    monkeypatch.setattr(controller, "update_traffic_weights", fake_promote)
    monkeypatch.setattr(controller, "send_alert", fake_send_alert)
    monkeypatch.setattr(controller, "alert_rollback", fake_alert_rollback)
    return calls


def test_rollback_proceeds_when_opa_is_unreachable(monkeypatch, _patch_actuation):
    """A FAILED verdict must still trigger a real rollback even if OPA can't be reached."""
    async def fake_opa_unreachable(payload):
        return {
            "allow_action": False,
            "require_human_approval": False,
            "rejection_reasons": ["OPA evaluation error: connection refused"],
            "raw_result": {},
            "opa_unreachable": True,
        }

    monkeypatch.setattr(controller, "evaluate_policy_async", fake_opa_unreachable)

    asyncio.run(controller.handle_incoming_verdict(_signed_payload("FAILED"), "run-failsafe-test"))

    assert len(_patch_actuation["rollback"]) == 1, "expected the rollback to proceed despite OPA being unreachable"
    assert _patch_actuation["rollback"][0]["authorized_by"] == "FAILSAFE:opa_unreachable"
    assert len(_patch_actuation["promote"]) == 0


def test_promotion_still_blocked_when_opa_is_unreachable(monkeypatch, _patch_actuation):
    """A HEALTHY verdict must NOT be promoted just because OPA can't be reached — fail-closed is correct here."""
    async def fake_opa_unreachable(payload):
        return {
            "allow_action": False,
            "require_human_approval": False,
            "rejection_reasons": ["OPA evaluation error: connection refused"],
            "raw_result": {},
            "opa_unreachable": True,
        }

    monkeypatch.setattr(controller, "evaluate_policy_async", fake_opa_unreachable)

    asyncio.run(controller.handle_incoming_verdict(_signed_payload("HEALTHY"), "run-failsafe-test"))

    assert len(_patch_actuation["promote"]) == 0, "promotion must stay blocked when OPA is unreachable"
    assert len(_patch_actuation["rollback"]) == 0


def test_rollback_stays_blocked_on_a_real_policy_rejection_not_just_unreachability(monkeypatch, _patch_actuation):
    """
    OPA being reached and legitimately saying "no" (e.g. a real guardrail
    rule) must NOT be treated the same as OPA being unreachable — the
    fail-safe override is only for the latter.
    """
    async def fake_opa_real_rejection(payload):
        return {
            "allow_action": False,
            "require_human_approval": False,
            "rejection_reasons": ["Some genuine policy rule rejected this"],
            "raw_result": {"allow_action": False},
            "opa_unreachable": False,
        }

    monkeypatch.setattr(controller, "evaluate_policy_async", fake_opa_real_rejection)

    asyncio.run(controller.handle_incoming_verdict(_signed_payload("FAILED"), "run-failsafe-test"))

    assert len(_patch_actuation["rollback"]) == 0, "a real (reachable) policy rejection must still be respected"
