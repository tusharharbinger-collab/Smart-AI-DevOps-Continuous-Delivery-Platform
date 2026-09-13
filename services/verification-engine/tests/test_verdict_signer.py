"""
services/verification-engine/tests/test_verdict_signer.py
"""
import json
import pytest
from src.verdict import ImmutableVerdict
from src.verdict_signer import sign_verdict, build_signed_payload


def _build_test_verdict(**kwargs) -> ImmutableVerdict:
    defaults = dict(
        pipeline_run_id="run-abc-123",
        status="HEALTHY",
        composite_score=92.5,
        confidence=0.94,
        evidence={"latency": {"p_value": 0.15}},
        tier1_breaches=None,
    )
    defaults.update(kwargs)
    return ImmutableVerdict.build(**defaults)


def test_sign_verdict_returns_64_char_hex():
    verdict = _build_test_verdict()
    sig = sign_verdict(verdict.canonical_json())
    assert isinstance(sig, str)
    assert len(sig) == 64   # SHA-256 in hex = 64 chars


def test_signature_different_for_different_verdicts():
    v1 = _build_test_verdict(status="HEALTHY")
    v2 = _build_test_verdict(status="FAILED")
    sig1 = sign_verdict(v1.canonical_json())
    sig2 = sign_verdict(v2.canonical_json())
    assert sig1 != sig2


def test_canonical_json_deterministic():
    verdict = _build_test_verdict()
    j1 = verdict.canonical_json()
    j2 = verdict.canonical_json()
    assert j1 == j2
    # Canonical JSON must be parseable
    parsed = json.loads(j1)
    assert parsed["status"] == "HEALTHY"
    assert parsed["pipeline_run_id"] == "run-abc-123"


def test_build_signed_payload_structure():
    verdict = _build_test_verdict()
    payload = build_signed_payload(verdict)
    assert "verdict" in payload
    assert "signature" in payload
    assert len(payload["signature"]) == 64


def test_frozen_verdict_cannot_be_mutated():
    verdict = _build_test_verdict()
    # Direct attribute assignment on a frozen dataclass MUST raise FrozenInstanceError
    # (which is a subclass of AttributeError)
    with pytest.raises((AttributeError, TypeError)):
        verdict.status = "FAILED"   # type: ignore[misc]  # intentional mutation attempt


def test_canonical_json_is_sorted_by_keys():
    verdict = _build_test_verdict()
    canonical = verdict.canonical_json()
    keys = list(json.loads(canonical).keys())
    assert keys == sorted(keys), "Canonical JSON fields must be sorted for determinism"
