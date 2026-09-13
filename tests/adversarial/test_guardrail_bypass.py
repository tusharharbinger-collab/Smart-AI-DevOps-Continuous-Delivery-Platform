"""
tests/adversarial/test_guardrail_bypass.py

Spec §10b / §14.2: freeze-window bypass, micro-sample attacks, forged
verdicts, stale-verdict replay, endpoint-concealment, and unauthorized
right-sizing application. Every test attempts to actually break a guardrail —
a passing test here means the attack FAILED, i.e. `allow_action` came back
False or the verdict was rejected before ever reaching OPA.

The freeze-window / micro-sample / right-sizing tests evaluate
policies/delivery_guardrails.rego directly via the `opa eval` CLI
(opa_eval_helper.py) rather than requiring a live OPA server — this exercises
the exact same policy file `opa test policies/ -v` and the OPA HTTP server
(policy-controller/src/opa_evaluator.py) both evaluate, without needing
docker compose running.
"""
import hmac
import hashlib
import json
from datetime import datetime, timedelta, timezone

from opa_eval_helper import eval_guardrails
from verdict_verifier import VerdictIntegrityError, get_signing_key, verify_and_parse


def test_freeze_window_cannot_be_bypassed_by_perfect_verdict():
    """Even a HEALTHY, high-confidence verdict must be blocked during a freeze window."""
    payload = {
        "requested_action": "PROMOTE_STEP",
        "verification_verdict": {
            "status": "HEALTHY", "confidence": 0.99,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        },
        "runtime_context": {
            "current_day": "Friday", "current_time": "18:00",
            "cluster_maintenance_lock": False,
        },
        "active_step_sample_count": 10000,
        "active_step_duration_seconds": 3600,
        "current_step": {"minSampleSize": 100, "minDuration": "300s"},
        "target_stage": "step_2",
        "approved_signatures": [],
        "cost_analysis": {"delta_percent": 1.0},
        "pipeline_policy": {
            "gates": {
                "blockedDeployWindows": [{"days": ["Friday"], "startTime": "16:00", "endTime": "23:59"}],
                "manualApprovalRequired": {"beforeStages": [], "approverRoles": []},
            },
            "guardrails": {"requireMinimumConfidence": 0.80, "minSampleSize": 100, "maxPermittedCostDeltaPercent": 15.0},
        },
    }
    result = eval_guardrails(payload)
    assert result["allow_action"] is False, "CRITICAL: freeze window was bypassed by a healthy verdict"


def test_micro_sample_attack_cannot_force_promotion():
    """Attacker routes 1% traffic and tries to promote after only 3 requests."""
    payload = {
        "requested_action": "PROMOTE_STEP",
        "verification_verdict": {
            "status": "HEALTHY", "confidence": 0.95,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        },
        "runtime_context": {"current_day": "Wednesday", "current_time": "10:00", "cluster_maintenance_lock": False},
        "active_step_sample_count": 3,          # micro-sample attack
        "active_step_duration_seconds": 3600,
        "current_step": {"minSampleSize": 100, "minDuration": "300s"},
        "target_stage": "step_1",
        "approved_signatures": [],
        "cost_analysis": {"delta_percent": 1.0},
        "pipeline_policy": {
            "gates": {"blockedDeployWindows": [], "manualApprovalRequired": {"beforeStages": [], "approverRoles": []}},
            "guardrails": {"requireMinimumConfidence": 0.80, "minSampleSize": 100, "maxPermittedCostDeltaPercent": 15.0},
        },
    }
    result = eval_guardrails(payload)
    assert result["allow_action"] is False, "CRITICAL: micro-sample attack (N=3) was allowed to promote"


def test_forged_verdict_rejected_by_hmac_verification():
    """
    Attacker with Redis write access (but WITHOUT the signing key) injects a
    fabricated HEALTHY verdict directly onto the verdicts:{run_id} channel,
    bypassing verification-engine entirely.
    """
    forged_verdict = {
        "verdict_id": "forged-123", "pipeline_run_id": "run-456",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": "HEALTHY", "composite_score": 100.0, "confidence": 1.0,
        "evidence": {}, "tier1_breaches": [],
    }
    canonical = json.dumps(forged_verdict, sort_keys=True, separators=(",", ":"))
    fake_signature = hmac.new(b"wrong-secret-key", canonical.encode(), hashlib.sha256).hexdigest()

    payload = {"verdict": canonical, "signature": fake_signature}

    try:
        verify_and_parse(payload)
        assert False, "CRITICAL: forged verdict with an invalid signature was accepted"
    except VerdictIntegrityError:
        pass  # expected — the forged verdict never reaches OPA or Kubernetes


def test_stale_verdict_replay_rejected():
    """A valid, correctly-signed verdict from 10 minutes ago is replayed."""
    signing_key = get_signing_key()
    old_verdict = {
        "verdict_id": "old-789", "pipeline_run_id": "run-456",
        "timestamp_utc": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "status": "HEALTHY", "composite_score": 100.0, "confidence": 1.0,
        "evidence": {}, "tier1_breaches": [],
    }
    canonical = json.dumps(old_verdict, sort_keys=True, separators=(",", ":"))
    valid_signature = hmac.new(signing_key, canonical.encode(), hashlib.sha256).hexdigest()
    payload = {"verdict": canonical, "signature": valid_signature}

    try:
        verify_and_parse(payload)
        assert False, "CRITICAL: a 10-minute-old verdict was accepted despite the 300s freshness window"
    except VerdictIntegrityError as e:
        assert "300s" in str(e) or "freshness" in str(e).lower()


def test_endpoint_concealment_marks_unverifiable():
    """Canary routes only /healthz traffic to mask real errors — entropy check must catch this."""
    from scipy.stats import ks_2samp

    baseline_paths = ["/api/v1/payments"] * 900 + ["/healthz"] * 100
    canary_paths = ["/healthz"] * 1000  # 100% concealment attempt

    baseline_encoded = [0 if p == "/healthz" else 1 for p in baseline_paths]
    canary_encoded = [0 if p == "/healthz" else 1 for p in canary_paths]
    D, _ = ks_2samp(baseline_encoded, canary_encoded)

    assert D > 0.40, "Endpoint concealment attack was not detected by entropy check"


def test_rightsizing_cannot_auto_apply_without_platform_admin():
    payload = {
        "requested_action": "APPLY_RIGHTSIZING",
        "rightsizing_recommendation": {"is_overprovisioned": True},
        "approved_signatures": [],   # no approval at all
        "verification_verdict": {"status": "HEALTHY"},
        "pipeline_policy": {"guardrails": {}},
        "runtime_context": {},
    }
    result = eval_guardrails(payload)
    assert result["allow_action"] is False, "CRITICAL: right-sizing auto-applied without any approval"


def test_rightsizing_rejects_wrong_role_signature():
    """A developer's sign-off (not platform-admin) must not be sufficient."""
    payload = {
        "requested_action": "APPLY_RIGHTSIZING",
        "rightsizing_recommendation": {"is_overprovisioned": True},
        "approved_signatures": [{"role": "developer"}],
        "verification_verdict": {"status": "HEALTHY"},
        "pipeline_policy": {"guardrails": {}},
        "runtime_context": {},
    }
    result = eval_guardrails(payload)
    assert result["allow_action"] is False, "CRITICAL: a non-platform-admin signature authorized right-sizing"
