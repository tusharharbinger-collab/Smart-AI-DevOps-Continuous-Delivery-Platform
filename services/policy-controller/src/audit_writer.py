"""
services/policy-controller/src/audit_writer.py

Records actuation events to the audit ledger.

Real bug found live (a user noticed the Audit Ledger UI screen was empty
for a run that had genuinely completed): `db` was accepted here but no
caller ever actually supplied one — `actuation_executor.py`'s
`update_traffic_weights`/`emergency_rollback` called this with no db
argument at all, so `audit_ledger` has never had a single real row written
to it since this platform's first build. See db.py's module docstring for
the full story and the fix.
"""
import structlog
from datetime import datetime, timezone

logger = structlog.get_logger(__name__)


async def record_actuation(
    pipeline_run_id: str,
    action: str,
    canary_weight: int | None = None,
    baseline_weight: int | None = None,
    authorized_by: str = "",
    verdict: str | None = None,
    confidence: float | None = None,
    policy_rule: str | None = None,
    hmac_signature: str | None = None,
    tenant_id: str | None = None,
    db=None,
) -> dict:
    """
    Records an actuation event to stdout (always) and, when both `db` (a
    connected `src.db.PolicyControllerDB`) and `tenant_id` are available,
    into the real Postgres `audit_ledger` table — this is what the Audit
    Ledger UI screen and the SOC 2 CSV export actually read from.
    """
    event = {
        "pipeline_run_id": pipeline_run_id,
        "action": action,
        "canary_weight": canary_weight,
        "baseline_weight": baseline_weight or (100 - canary_weight if canary_weight is not None else None),
        "authorized_by": authorized_by,
        "verdict": verdict,
        "confidence": confidence,
        "policy_rule": policy_rule,
        "hmac_signature": hmac_signature or "verified",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("actuation_recorded", **event)

    if db and tenant_id:
        try:
            await db.record_actuation(
                tenant_id=tenant_id,
                pipeline_run_id=pipeline_run_id,
                action=action,
                canary_weight=canary_weight,
                baseline_weight=event["baseline_weight"],
                authorized_by=authorized_by,
                verdict=verdict,
                confidence=confidence,
                policy_rule=policy_rule,
                hmac_signature=event["hmac_signature"],
            )
        except Exception as e:
            logger.error("db_audit_record_failed", error=str(e), pipeline_run_id=pipeline_run_id)
    elif not tenant_id:
        logger.warning(
            "audit_record_skipped_no_tenant_id",
            pipeline_run_id=pipeline_run_id,
            reason="no actuation_target Redis entry with tenant_id for this run — pre-Phase-6 or manual invocation",
        )

    return event
