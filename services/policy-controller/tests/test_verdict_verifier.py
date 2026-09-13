"""
Unit tests for policy-controller verdict verification (§8.3, §14.2).
"""
import os
import json
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
import pytest
import sys

# Add policy-controller root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.verdict_verifier import verify_and_parse, VerdictIntegrityError, get_signing_key


def _build_signed_payload(verdict: dict, secret_key: bytes | None = None) -> dict:
    key = secret_key or get_signing_key()
    canonical = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
    return {"verdict": canonical, "signature": sig}


def test_valid_verdict_verification_passes():
    now_str = datetime.now(timezone.utc).isoformat()
    verdict = {
        "verdict_id": "v-100",
        "pipeline_run_id": "run-200",
        "timestamp_utc": now_str,
        "status": "HEALTHY",
        "composite_score": 95.0,
        "confidence": 0.92,
    }
    payload = _build_signed_payload(verdict)
    parsed = verify_and_parse(payload)
    assert parsed["verdict_id"] == "v-100"
    assert parsed["status"] == "HEALTHY"


def test_forged_verdict_signature_rejected():
    now_str = datetime.now(timezone.utc).isoformat()
    verdict = {
        "verdict_id": "v-forged",
        "pipeline_run_id": "run-bad",
        "timestamp_utc": now_str,
        "status": "HEALTHY",
    }
    payload = _build_signed_payload(verdict, secret_key=b"wrong-attacker-key")
    with pytest.raises(VerdictIntegrityError, match="HMAC signature verification failed"):
        verify_and_parse(payload)


def test_stale_verdict_rejected():
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=350)).isoformat()
    verdict = {
        "verdict_id": "v-stale",
        "pipeline_run_id": "run-old",
        "timestamp_utc": stale_time,
        "status": "HEALTHY",
    }
    payload = _build_signed_payload(verdict)
    with pytest.raises(VerdictIntegrityError, match="exceeds 300s freshness window"):
        verify_and_parse(payload)


def test_verdict_signed_with_previous_key_accepted_during_rotation(monkeypatch):
    """
    Phase 4 (§04-security-hardening.md, 4.4): during a key rotation,
    verification-engine may still be signing with the outgoing key for a
    moment after policy-controller has already switched its OWN
    VERDICT_SIGNING_KEY to the new value — as long as VERDICT_SIGNING_KEY_
    PREVIOUS is set to that outgoing key, such a verdict must still verify
    rather than being wrongly rejected as forged.
    """
    old_key = b"outgoing-key-being-rotated-out"
    monkeypatch.setenv("VERDICT_SIGNING_KEY", "brand-new-rotated-in-key")
    monkeypatch.setenv("VERDICT_SIGNING_KEY_PREVIOUS", old_key.decode())

    now_str = datetime.now(timezone.utc).isoformat()
    verdict = {
        "verdict_id": "v-rotation",
        "pipeline_run_id": "run-rotation",
        "timestamp_utc": now_str,
        "status": "HEALTHY",
    }
    payload = _build_signed_payload(verdict, secret_key=old_key)
    parsed = verify_and_parse(payload)
    assert parsed["verdict_id"] == "v-rotation"


def test_verdict_signed_with_stale_key_rejected_once_previous_key_not_configured(monkeypatch):
    """Without VERDICT_SIGNING_KEY_PREVIOUS set, a verdict signed with any key
    other than the current one is still correctly rejected — rotation
    support must not weaken the default (no-grace-period) behavior."""
    old_key = b"outgoing-key-being-rotated-out"
    monkeypatch.setenv("VERDICT_SIGNING_KEY", "brand-new-rotated-in-key")
    monkeypatch.delenv("VERDICT_SIGNING_KEY_PREVIOUS", raising=False)

    now_str = datetime.now(timezone.utc).isoformat()
    verdict = {
        "verdict_id": "v-no-grace",
        "pipeline_run_id": "run-no-grace",
        "timestamp_utc": now_str,
        "status": "HEALTHY",
    }
    payload = _build_signed_payload(verdict, secret_key=old_key)
    with pytest.raises(VerdictIntegrityError, match="HMAC signature verification failed"):
        verify_and_parse(payload)
