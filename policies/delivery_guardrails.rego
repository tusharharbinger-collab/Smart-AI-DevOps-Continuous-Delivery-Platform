# policies/delivery_guardrails.rego
package delivery.guardrails

import future.keywords.in

default allow_action = false
default require_human_approval = false

verdict := input.verification_verdict
policy := input.pipeline_policy
context := input.runtime_context

# =====================================================================
# RULE 1: Autonomous Rollback Authorization
# =====================================================================
allow_action {
    input.requested_action == "ROLLBACK"
    verdict.status == "FAILED"
    "FAILED" in policy.guardrails.autoRollbackOnVerdict
    not is_emergency_freeze_active
}

# =====================================================================
# RULE 2: Autonomous Progressive Promotion Authorization
# =====================================================================
allow_action {
    input.requested_action == "PROMOTE_STEP"
    verdict.status == "HEALTHY"
    verdict.confidence >= policy.guardrails.requireMinimumConfidence
    input.active_step_sample_count >= max([input.current_step.minSampleSize, policy.guardrails.minSampleSize])
    input.active_step_duration_seconds >= parse_duration_seconds(input.current_step.minDuration)
    not is_deploy_window_blocked
    not is_manual_approval_pending
    not verdict_is_stale
    not cost_delta_exceeds_limit
}

# =====================================================================
# RULE 10: Blue-Green Cutover Authorization
# =====================================================================
allow_action {
    input.requested_action == "BLUE_GREEN_CUTOVER"
    verdict.status == "HEALTHY"
    verdict.confidence >= policy.guardrails.requireMinimumConfidence
    input.active_step_sample_count >= max([input.current_step.minSampleSize, policy.guardrails.minSampleSize])
    input.active_step_duration_seconds >= parse_duration_seconds(input.current_step.minDuration)
    not is_deploy_window_blocked
    not is_manual_approval_pending
    not verdict_is_stale
    not cost_delta_exceeds_limit
}


# =====================================================================
# RULE 3: Blocked Deploy Freeze Windows
# (Friday after 16:00 UTC through end of weekend, per assignment example)
# =====================================================================
is_deploy_window_blocked {
    some window in policy.gates.blockedDeployWindows
    context.current_day in window.days
    context.current_time >= window.startTime
    context.current_time <= window.endTime
}

# =====================================================================
# RULE 4: Mandatory Manual Sign-off Check
# =====================================================================
is_manual_approval_pending {
    input.target_stage in policy.gates.manualApprovalRequired.beforeStages
    count(input.approved_signatures) == 0
}

require_human_approval {
    is_manual_approval_pending
}

# =====================================================================
# RULE 5: Emergency Cluster Maintenance Lock
# =====================================================================
is_emergency_freeze_active {
    context.cluster_maintenance_lock == true
}

# =====================================================================
# RULE 6: Verdict Freshness (replay-attack defense — see §8.3 for the
# cryptographic layer; this is the independent policy-level check)
# =====================================================================
verdict_is_stale {
    now_ns := time.now_ns()
    verdict_ns := time.parse_rfc3339_ns(verdict.timestamp_utc)
    (now_ns - verdict_ns) > (300 * 1000000000)   # 300 seconds in nanoseconds
}

# =====================================================================
# RULE 7: Cost Guardrail — blocks promotion if it would exceed the
# configured maximum permitted compute-cost delta
# =====================================================================
cost_delta_exceeds_limit {
    input.cost_analysis.delta_percent > policy.guardrails.maxPermittedCostDeltaPercent
}

# =====================================================================
# RULE 9: First-Deployment Authorization
# A project's genuinely first-ever deployment has no prior baseline to
# compare against, so PROMOTE_STEP's statistical gates (confidence, sample
# size, verdict freshness) are a category error for it — see worker.py's
# canary_loop first-deployment branch. The ONE guardrail that still
# meaningfully applies even with zero statistical evidence is the freeze
# window: you don't want a first deployment auto-promoting during a
# declared no-deploy window any more than a normal one. Deliberately its
# own requested_action rather than overloading PROMOTE_STEP, so this rule
# can never accidentally authorize a real canary promotion that skipped
# its actual evidence requirements.
# =====================================================================
allow_action {
    input.requested_action == "FIRST_DEPLOYMENT"
    not is_deploy_window_blocked
    not is_emergency_freeze_active
}

rejection_reasons[reason] {
    input.requested_action == "FIRST_DEPLOYMENT"
    is_deploy_window_blocked
    reason := "Current timestamp falls within an enterprise-blocked deployment window"
}

# =====================================================================
# RULE 11: Health-Gated Cutover Authorization (blue-green)
# Blue-green's whole point is to guarantee a live URL even for a project
# with zero real traffic — a genuinely new web app can never accumulate
# PROMOTE_STEP/BLUE_GREEN_CUTOVER's required sample count, so gating on a
# statistical verdict here is a category error, exactly like
# FIRST_DEPLOYMENT's own reasoning above. The caller (worker.py's blue-green
# rollout branch) has already confirmed real infrastructure health — ECS
# task stability AND a real ALB target-group HTTP health check — before
# ever requesting this action, so the one guardrail that still meaningfully
# applies with zero statistical evidence is, again, the freeze window.
# Deliberately its own requested_action (not reusing RULE 10's
# BLUE_GREEN_CUTOVER, which stays verdict-gated for any caller that still
# wants a statistically-informed cutover) so this rule can never
# accidentally authorize a real canary promotion that skipped its actual
# evidence requirements.
# =====================================================================
allow_action {
    input.requested_action == "HEALTH_GATED_CUTOVER"
    not is_deploy_window_blocked
    not is_emergency_freeze_active
}

rejection_reasons[reason] {
    input.requested_action == "HEALTH_GATED_CUTOVER"
    is_deploy_window_blocked
    reason := "Current timestamp falls within an enterprise-blocked deployment window"
}

# =====================================================================
# RULE 8: Autonomous Right-Sizing Application Gate
# (§6d / §10.3 — right-sizing recommendations NEVER auto-apply; this
# rule only ever fires for a distinct, explicitly-approved action type)
# =====================================================================
allow_action {
    input.requested_action == "APPLY_RIGHTSIZING"
    input.rightsizing_recommendation.is_overprovisioned == true
    count(input.approved_signatures) > 0          # always requires a human sign-off
    "platform-admin" in [s.role | some s in input.approved_signatures]
}

# =====================================================================
# Human-readable rejection reasons (surfaced verbatim in the UI and in
# the Action Decision Report, §12.2)
# =====================================================================
rejection_reasons[reason] {
    input.requested_action in ["PROMOTE_STEP", "BLUE_GREEN_CUTOVER"]
    verdict.status != "HEALTHY"
    reason := sprintf("Verification verdict is %v; promotion/cutover requires HEALTHY", [verdict.status])
}

rejection_reasons[reason] {
    input.requested_action in ["PROMOTE_STEP", "BLUE_GREEN_CUTOVER"]
    verdict.confidence < policy.guardrails.requireMinimumConfidence
    reason := sprintf("Confidence %v is below required threshold %v",
        [verdict.confidence, policy.guardrails.requireMinimumConfidence])
}

rejection_reasons[reason] {
    input.active_step_sample_count < policy.guardrails.minSampleSize
    reason := sprintf("Sample count %v is below the required minimum of %v",
        [input.active_step_sample_count, policy.guardrails.minSampleSize])
}

rejection_reasons[reason] {
    is_deploy_window_blocked
    reason := "Current timestamp falls within an enterprise-blocked deployment window"
}

rejection_reasons[reason] {
    is_manual_approval_pending
    reason := sprintf("Stage %v requires manual approval from one of: %v",
        [input.target_stage, policy.gates.manualApprovalRequired.approverRoles])
}

rejection_reasons[reason] {
    verdict_is_stale
    reason := "Verdict timestamp exceeds the 300-second freshness window (possible replay)"
}

rejection_reasons[reason] {
    cost_delta_exceeds_limit
    reason := sprintf("Cost delta %v%% exceeds the permitted maximum of %v%%",
        [input.cost_analysis.delta_percent, policy.guardrails.maxPermittedCostDeltaPercent])
}

# Helper: parse "300s" / "5m" style duration strings into seconds
parse_duration_seconds(duration_str) = seconds {
    endswith(duration_str, "s")
    seconds := to_number(trim_suffix(duration_str, "s"))
}
parse_duration_seconds(duration_str) = seconds {
    endswith(duration_str, "m")
    seconds := to_number(trim_suffix(duration_str, "m")) * 60
}
