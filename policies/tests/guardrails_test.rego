# policies/tests/guardrails_test.rego
package delivery.guardrails

test_rollback_allowed_on_failed_verdict {
    allow_action with input as {
        "requested_action": "ROLLBACK",
        "verification_verdict": {"status": "FAILED"},
        "pipeline_policy": {"guardrails": {"autoRollbackOnVerdict": ["FAILED"]}},
        "runtime_context": {"cluster_maintenance_lock": false}
    }
}

test_first_deployment_allowed_outside_freeze_window {
    allow_action with input as {
        "requested_action": "FIRST_DEPLOYMENT",
        "runtime_context": {"current_day": "Tuesday", "current_time": "10:00", "cluster_maintenance_lock": false},
        "pipeline_policy": {
            "gates": {"blockedDeployWindows": [
                {"days": ["Friday", "Saturday", "Sunday"], "startTime": "16:00", "endTime": "23:59"}
            ]}
        }
    }
}

test_first_deployment_blocked_during_freeze_window {
    not allow_action with input as {
        "requested_action": "FIRST_DEPLOYMENT",
        "runtime_context": {"current_day": "Friday", "current_time": "17:00", "cluster_maintenance_lock": false},
        "pipeline_policy": {
            "gates": {"blockedDeployWindows": [
                {"days": ["Friday", "Saturday", "Sunday"], "startTime": "16:00", "endTime": "23:59"}
            ]}
        }
    }
}

test_first_deployment_blocked_during_emergency_maintenance {
    not allow_action with input as {
        "requested_action": "FIRST_DEPLOYMENT",
        "runtime_context": {"current_day": "Tuesday", "current_time": "10:00", "cluster_maintenance_lock": true},
        "pipeline_policy": {"gates": {"blockedDeployWindows": []}}
    }
}

test_promotion_blocked_during_friday_freeze {
    not allow_action with input as {
        "requested_action": "PROMOTE_STEP",
        "verification_verdict": {"status": "HEALTHY", "confidence": 0.95, "timestamp_utc": "2026-09-11T17:00:00Z"},
        "runtime_context": {"current_day": "Friday", "current_time": "17:00", "cluster_maintenance_lock": false},
        "active_step_sample_count": 500,
        "active_step_duration_seconds": 400,
        "current_step": {"minSampleSize": 100, "minDuration": "300s"},
        "target_stage": "step_2",
        "approved_signatures": [],
        "cost_analysis": {"delta_percent": 2.0},
        "pipeline_policy": {
            "gates": {"blockedDeployWindows": [
                {"days": ["Friday"], "startTime": "16:00", "endTime": "23:59"}
            ], "manualApprovalRequired": {"beforeStages": [], "approverRoles": []}},
            "guardrails": {"requireMinimumConfidence": 0.80, "minSampleSize": 100, "maxPermittedCostDeltaPercent": 15.0}
        }
    }
}

test_promotion_blocked_below_minimum_sample_size {
    not allow_action with input as {
        "requested_action": "PROMOTE_STEP",
        "verification_verdict": {"status": "HEALTHY", "confidence": 0.95, "timestamp_utc": "2026-09-12T10:00:00Z"},
        "runtime_context": {"current_day": "Saturday", "current_time": "10:00", "cluster_maintenance_lock": false},
        "active_step_sample_count": 8,
        "active_step_duration_seconds": 400,
        "current_step": {"minSampleSize": 100, "minDuration": "300s"},
        "target_stage": "step_1",
        "approved_signatures": [],
        "cost_analysis": {"delta_percent": 2.0},
        "pipeline_policy": {
            "gates": {"blockedDeployWindows": [], "manualApprovalRequired": {"beforeStages": [], "approverRoles": []}},
            "guardrails": {"requireMinimumConfidence": 0.80, "minSampleSize": 100, "maxPermittedCostDeltaPercent": 15.0}
        }
    }
}

test_rightsizing_requires_platform_admin_signature {
    not allow_action with input as {
        "requested_action": "APPLY_RIGHTSIZING",
        "rightsizing_recommendation": {"is_overprovisioned": true},
        "approved_signatures": [{"role": "developer"}],   # wrong role -> denied
        "verification_verdict": {"status": "HEALTHY"},
        "pipeline_policy": {"guardrails": {}},
        "runtime_context": {}
    }
}
