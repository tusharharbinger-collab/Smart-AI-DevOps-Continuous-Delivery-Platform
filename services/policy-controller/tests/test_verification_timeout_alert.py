"""
services/policy-controller/tests/test_verification_timeout_alert.py

Assignment requirement: "the platform must proactively notify the right
people the moment it acts or needs one — at minimum when ... verification
can't reach a confident verdict in the allotted time." An UNVERIFIABLE
verdict (verification-engine unreachable, or no confident HEALTHY/FAILED
call could be made in the allotted window) previously fell all the way
through `handle_incoming_verdict` with no actuation AND no alert —
`alert_verification_timeout` existed in `alert_dispatcher.py` since Phase 6
but was never called anywhere. These tests verify: (1) the alert now fires
with the right sample counts, (2) OPA is never even consulted for an
UNVERIFIABLE verdict (asking "can I PROMOTE_STEP?" for a verdict that isn't
requesting any action is a category error — and the real Rego policy would
reject it anyway, as a generic BLOCKED action, which is a materially
different alert), and (3) no actuation (no promote, no rollback) is ever
triggered on inconclusive evidence.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from src import controller
from src.verdict_verifier import get_signing_key


def _signed_unverifiable_payload(evidence: dict | None = None, note: str = "verification-engine timeout") -> str:
    verdict = {
        "verdict_id": "v-unverifiable",
        "pipeline_run_id": "run-timeout-test",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "UNVERIFIABLE",
        "confidence": 0.0,
        "note": note,
        "evidence": evidence or {},
    }
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(get_signing_key(), canonical.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"verdict": canonical, "signature": sig})


@pytest.fixture(autouse=True)
def _patch_actuation(monkeypatch):
    """Never let this test suite touch a real Kubernetes API or send a real alert."""
    calls = {"rollback": [], "promote": [], "timeout_alert": [], "opa_calls": 0}

    async def fake_rollback(pipeline_run_id, authorized_by, **kwargs):
        calls["rollback"].append({"pipeline_run_id": pipeline_run_id, "authorized_by": authorized_by, **kwargs})

    async def fake_promote(pipeline_run_id, canary_weight, baseline_weight, authorized_by, **kwargs):
        calls["promote"].append({"pipeline_run_id": pipeline_run_id, "authorized_by": authorized_by})

    async def fake_send_alert(*args, **kwargs):
        pass

    async def fake_alert_verification_timeout(pipeline_run_id, samples_collected, samples_required):
        calls["timeout_alert"].append(
            {"pipeline_run_id": pipeline_run_id, "samples_collected": samples_collected, "samples_required": samples_required}
        )

    async def fake_opa_never_called(payload):
        calls["opa_calls"] += 1
        raise AssertionError("OPA must never be consulted for an UNVERIFIABLE verdict")

    monkeypatch.setattr(controller, "emergency_rollback", fake_rollback)
    monkeypatch.setattr(controller, "update_traffic_weights", fake_promote)
    monkeypatch.setattr(controller, "send_alert", fake_send_alert)
    monkeypatch.setattr(controller, "alert_verification_timeout", fake_alert_verification_timeout)
    monkeypatch.setattr(controller, "evaluate_policy_async", fake_opa_never_called)
    return calls


def test_unverifiable_verdict_fires_timeout_alert_without_consulting_opa(_patch_actuation):
    payload = _signed_unverifiable_payload(evidence={"error_rate": {"total_requests": 42}})

    asyncio.run(controller.handle_incoming_verdict(payload, "run-timeout-test"))

    assert _patch_actuation["opa_calls"] == 0, "OPA must be skipped entirely for an UNVERIFIABLE verdict"
    assert len(_patch_actuation["timeout_alert"]) == 1
    assert _patch_actuation["timeout_alert"][0]["pipeline_run_id"] == "run-timeout-test"
    assert _patch_actuation["timeout_alert"][0]["samples_collected"] == 42
    assert _patch_actuation["timeout_alert"][0]["samples_required"] == 100


def test_unverifiable_verdict_never_triggers_promotion_or_rollback(_patch_actuation):
    payload = _signed_unverifiable_payload()

    asyncio.run(controller.handle_incoming_verdict(payload, "run-timeout-test"))

    assert len(_patch_actuation["promote"]) == 0, "inconclusive evidence must never auto-promote"
    assert len(_patch_actuation["rollback"]) == 0, "inconclusive evidence must never auto-rollback"


def test_unverifiable_verdict_uses_policy_min_sample_size_for_required_count(_patch_actuation):
    payload = _signed_unverifiable_payload(evidence={"latency": {"mann_whitney": {"n_canary": 17}}})
    custom_policy = {
        "gates": {"blockedDeployWindows": [], "manualApprovalRequired": {"beforeStages": [], "approverRoles": []}},
        "guardrails": {"minSampleSize": 250},
    }

    asyncio.run(controller.handle_incoming_verdict(payload, "run-timeout-test", pipeline_policy=custom_policy))

    assert _patch_actuation["timeout_alert"][0]["samples_collected"] == 17
    assert _patch_actuation["timeout_alert"][0]["samples_required"] == 250
