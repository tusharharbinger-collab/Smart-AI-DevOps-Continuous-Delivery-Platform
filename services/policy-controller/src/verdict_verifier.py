"""
services/policy-controller/src/verdict_verifier.py

HMAC-SHA256 verification and replay-attack defense on incoming verdicts.
The secret key is shared only between verification-engine and policy-controller.
"""
import hmac
import hashlib
import json
import os
from datetime import datetime, timezone
import structlog

logger = structlog.get_logger(__name__)


def get_signing_key() -> bytes:
    return os.environ.get("VERDICT_SIGNING_KEY", "dev-secret-key-change-in-prod").encode()


def get_previous_signing_key() -> bytes | None:
    """
    Phase 4 (§04-security-hardening.md, deliverable 4.4) — key rotation
    support for a secret shared between two services. Rotating
    VERDICT_SIGNING_KEY naively (bump it on both services simultaneously)
    has a window where a verdict verification-engine signed a moment ago
    with the OLD key arrives at policy-controller after it's already
    switched to the NEW key — that verdict would be wrongly rejected as
    forged. Setting VERDICT_SIGNING_KEY_PREVIOUS to the outgoing key during
    a rotation lets policy-controller accept either key for a grace period;
    remove the env var once nothing signed with the old key is still in
    flight (comfortably longer than the 300s staleness window below).
    """
    raw = os.environ.get("VERDICT_SIGNING_KEY_PREVIOUS")
    return raw.encode() if raw else None


class VerdictIntegrityError(Exception):
    pass


def verify_and_parse(payload: dict) -> dict:
    """
    Recomputes the HMAC and compares in constant time. Rejects (raises) on any
    mismatch — stops forged/replayed verdicts from ever reaching OPA.
    Also enforces staleness: verdicts older than 5 minutes are rejected.
    """
    canonical = payload.get("verdict", "")
    claimed_signature = payload.get("signature", "")

    candidate_keys = [("current", get_signing_key())]
    previous_key = get_previous_signing_key()
    if previous_key is not None:
        candidate_keys.append(("previous", previous_key))

    matched_key = None
    for key_label, key in candidate_keys:
        expected_signature = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(claimed_signature, expected_signature):
            matched_key = key_label
            break

    if matched_key is None:
        logger.error("verdict_signature_mismatch", canonical_preview=canonical[:100])
        raise VerdictIntegrityError("HMAC signature verification failed — possible forged verdict")

    verdict = json.loads(canonical)

    if matched_key == "previous":
        # Not an error — this is the grace period working as intended — but
        # worth surfacing so an operator can tell when it's safe to drop
        # VERDICT_SIGNING_KEY_PREVIOUS (once this stops appearing in logs).
        logger.warning("verdict_verified_with_previous_signing_key", verdict_id=verdict.get("verdict_id"))

    timestamp_str = verdict.get("timestamp_utc", "")
    if timestamp_str:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_seconds > 300:
            logger.error("verdict_stale", age_seconds=age_seconds, verdict_id=verdict.get("verdict_id"))
            raise VerdictIntegrityError(f"Verdict is {age_seconds:.0f}s old — exceeds 300s freshness window")

    logger.info("verdict_verified", verdict_id=verdict.get("verdict_id"), status=verdict.get("status"))
    return verdict
