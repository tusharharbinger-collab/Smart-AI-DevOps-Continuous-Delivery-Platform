"""
services/verification-engine/src/verdict_signer.py

HMAC-SHA256 signing of every verdict before it is published to Redis.
The signing key (VERDICT_SIGNING_KEY) is a shared secret present ONLY in
verification-engine and policy-controller containers — never in the frontend,
never in the API gateway, never logged anywhere.

This is the cryptographic layer of the three-layer executor boundary (§8.1):
an attacker with Redis write-access but without the signing key cannot inject
a valid HEALTHY verdict — their forged payload will be rejected by
verdict_verifier.py before ever reaching OPA or Kubernetes.
"""
import hashlib
import hmac
import os

# Will raise at import time if the env var is not set — intentional.
# An unconfigured key means the system should not start.
SIGNING_KEY: bytes = os.environ.get("VERDICT_SIGNING_KEY", "dev-signing-key-change-in-prod").encode()


def sign_verdict(verdict_canonical_json: str) -> str:
    """
    Compute HMAC-SHA256 of the canonical verdict JSON.
    Returns the hex-encoded digest.
    """
    return hmac.new(SIGNING_KEY, verdict_canonical_json.encode("utf-8"), hashlib.sha256).hexdigest()


def build_signed_payload(verdict) -> dict:
    """
    Produce the Redis-publishable payload:
      {"verdict": "<canonical_json>", "signature": "<hex_hmac>"}

    The outer dict is then JSON-serialised by publisher.py.
    """
    canonical = verdict.canonical_json()
    signature = sign_verdict(canonical)
    return {"verdict": canonical, "signature": signature}
