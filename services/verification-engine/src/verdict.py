"""
services/verification-engine/src/verdict.py

ImmutableVerdict — frozen dataclass representing a verification decision.
Immutability is enforced at the Python level (frozen=True) and at the
cryptographic level (HMAC in verdict_signer.py). The combination prevents
both accidental and adversarial mutation.
"""
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Literal
import uuid


def _json_safe(obj):
    """
    Real bug found live, in three different places before this single fix:
    a statistical test's evidence can legitimately contain a non-finite
    float — Fisher's exact test's odds-ratio `statistic` is mathematically
    infinite whenever a contingency-table cell is exactly 0, a real, valid
    result, not an error. Python's own `json.dumps` happily emits the
    literal `Infinity`/`NaN` tokens for these (non-standard but permitted
    unless `allow_nan=False`), which then broke: OPA's strict JSON decoder
    (400 Bad Request), Postgres's JSONB column (rejects the same tokens),
    and Starlette's JSONResponse (`allow_nan=False` by default — this is
    what actually 500'd the live Verification Inspector for a real user).
    Sanitizing once, here, at the one place every verdict is constructed —
    `ImmutableVerdict.build()` — means no caller downstream needs its own
    copy of this fix ever again.
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


@dataclass(frozen=True)
class ImmutableVerdict:
    verdict_id: str
    pipeline_run_id: str
    timestamp_utc: str
    status: Literal["HEALTHY", "DEGRADED", "FAILED", "UNVERIFIABLE"]
    composite_score: float
    confidence: float
    evidence: dict            # frozen at construction; not mutated after
    tier1_breaches: tuple     # tuple (not list) to keep the dataclass hashable

    def canonical_json(self) -> str:
        """
        Deterministic serialization — required so the signer and verifier
        hash exactly the same bytes regardless of field order in the object.
        """
        d = asdict(self)
        # Convert tuple → list for JSON (asdict already does this for tuples)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @classmethod
    def build(
        cls,
        pipeline_run_id: str,
        status: str,
        composite_score: float,
        confidence: float,
        evidence: dict,
        tier1_breaches: list[str] | None = None,
    ) -> "ImmutableVerdict":
        """Factory — auto-generates verdict_id and timestamp."""
        return cls(
            verdict_id=str(uuid.uuid4()),
            pipeline_run_id=pipeline_run_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            status=status,
            composite_score=round(float(composite_score), 2),
            confidence=round(float(confidence), 4),
            evidence=_json_safe(evidence),
            tier1_breaches=tuple(tier1_breaches or []),
        )
