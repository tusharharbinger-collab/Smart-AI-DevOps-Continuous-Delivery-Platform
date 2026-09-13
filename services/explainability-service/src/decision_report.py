"""
services/explainability-service/src/decision_report.py

Report 2: Action Decision Report — spec §12.2. Distinct from Report 1
(reports_router.get_deployment_report, which is about the verification
*comparison*): this report is about the action *taken* and which policy
clause authorized or blocked it.
"""
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class ActionDecisionReport:
    decision_id: str
    pipeline_run_id: str
    triggering_verdict_id: str
    action_taken: str              # "ROLLBACK" | "PROMOTE_STEP" | "BLOCKED" | "APPROVAL_REQUIRED"
    specific_trigger: str          # e.g. "Tier-1 critical breach: http_error_rate"
    policy_rule_evaluated: str     # e.g. "delivery.guardrails.allow_action (Rule 1)"
    opa_rejection_reasons: list[str]
    authorized_by: str
    timestamp_utc: str
    hmac_signature: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_decision_report(
    pipeline_run_id: str,
    verdict: dict,
    opa_result: dict,
    action_taken: str,
) -> ActionDecisionReport:
    tier1_breaches = verdict.get("tier1_breaches") or []
    specific_trigger = (
        f"Tier-1 breach: {tier1_breaches}"
        if tier1_breaches
        else f"Composite score {verdict.get('composite_score', 0):.1f}"
    )
    return ActionDecisionReport(
        decision_id=str(uuid.uuid4()),
        pipeline_run_id=pipeline_run_id,
        triggering_verdict_id=verdict.get("verdict_id", ""),
        action_taken=action_taken,
        specific_trigger=specific_trigger,
        policy_rule_evaluated=opa_result.get("matched_rule", "unknown"),
        opa_rejection_reasons=opa_result.get("rejection_reasons", []),
        authorized_by=opa_result.get("authorized_by", "OPA"),
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        hmac_signature=opa_result.get("signature", ""),
    )
