# Right-Sizing Apply Flow — Plan

**Status: plan only, nothing in this file has been implemented yet.**

## Why this plan exists (and why it's scoped narrower than first proposed)

The original ask was "wire real CPU/memory telemetry into right-sizing recommendations" — CLAUDE.md's
"Known limitations" #8 and this repo's README both say that's still missing. **That statement is stale.**
Investigating before writing this plan turned up that commit `2c6e804` ("AI infra agent - independent cost
and OPA policy, BYO-AWS, failure RCA, right-sizing") already did that work, for both deploy targets:

- **Kubernetes**: `services/policy-controller/src/cost_tracker.py::compute_and_record_cost` calls
  `_fetch_p95_usage()`, a real Prometheus query (`container_cpu_usage_seconds_total`,
  `container_memory_usage_bytes`) against the canary Deployment, and feeds it into
  `compute_rightsizing_recommendation()`.
- **AWS ECS**: `services/policy-controller/src/cost_tracker_ecs.py::compute_ecs_rightsizing` pulls real
  CloudWatch `CPUUtilization`/`MemoryUtilization` datapoints for the **baseline** service (deliberately not
  the canary — a 10%-traffic canary always looks over-provisioned), computes a p95, and snaps the raw
  recommendation to a real deployable Fargate size (`snap_to_fargate_size`).
- Both write `rightsizing_rec` into `cost_analysis` (fail-soft: `None` when there isn't enough evidence yet
  — `RIGHTSIZING_MIN_SAMPLES`, default 10, for ECS).
- `frontend/src/pages/CostTab.tsx` already renders it: an "Over-provisioned" / "Right-sized" badge and the
  recommended size, sourced from `latestRec` in the cost history.
- `services/policy-controller/tests/test_ecs_rightsizing.py` covers the ECS path with a fake CloudWatch
  client (insufficient-samples, snapping, DB write).

So the telemetry-and-recommendation half of "right-sizing" is done and already live-verified via unit
tests. **The real remaining gap, found while checking this**: the recommendation is read-only. There is a
whole OPA rule reserved for approving and applying it —

```rego
# RULE 8: Autonomous Right-Sizing Application Gate
allow_action {
    input.requested_action == "APPLY_RIGHTSIZING"
    input.rightsizing_recommendation.is_overprovisioned == true
    count(input.approved_signatures) > 0
    "platform-admin" in [s.role | some s in input.approved_signatures]
}
```

— and `services/api-gateway/src/auth/rbac.py`'s own module docstring explicitly references this rule as a
reason role-checking exists at all. But nothing in the codebase ever sends `requested_action:
"APPLY_RIGHTSIZING"` to OPA. `grep -rl "APPLY_RIGHTSIZING"` across `services/` and `frontend/src/` matches
only the `.rego` file itself. A human can see "you're over-provisioned, resize to 0.25 vCPU / 0.5 GiB" in
the Cost tab and has no way to act on it through the platform — they'd have to hand-edit the Deployment
YAML or task definition outside the system entirely, un-audited.

**This plan is for closing that one gap: making the existing recommendation actionable, end to end,
through the same guardrail that already exists for it.**

## Non-goals

- Not re-deriving the recommendation math, the Prometheus/CloudWatch queries, or the Cost tab display —
  all real and correct already.
- Not automatic/autonomous application. Rule 8 requires a `platform-admin` signature by design (`§6d/§10.3`
  in the rego comment) — this plan keeps that. "Propose, human approves, then apply" matches this
  platform's existing invariant for every other AI-adjacent decision (infra drafts, pipeline tuning).
- Not extending right-sizing to the canary — deliberately measured on baseline only, for the reason already
  documented in `cost_tracker_ecs.py`.
- Not a scheduled/automatic re-check loop. Applying happens from whatever the latest stored
  `rightsizing_rec` is when a human clicks Apply; a stale recommendation is a data-freshness problem the
  existing `computed_at` timestamp already exposes in the UI, not something this plan needs to solve.

## Design

### 1. A new internal actuation endpoint per deploy target

Mirror the existing `graduate_canary`/`emergency_rollback` shape (`services/policy-controller/src/
actuation_executor.py`) and the ECS analog conventions in `shared/aws_ecs_actuation.py`:

- **Kubernetes**: `apply_rightsizing(pipeline_run_id, baseline_deployment_name, namespace,
  recommended_cpu_vcpu, recommended_mem_gib, authorized_by, tenant_id, db)` — a strategic-merge
  `patch_namespaced_deployment` on the baseline Deployment's container `resources.requests` (and
  `resources.limits`, kept equal to requests as the rest of this codebase already does for baseline/canary
  parity). Idempotent: re-applying the same numbers is a no-op patch.
- **ECS**: reuse `shared/aws_ecs_actuation.py::register_task_definition` to register a new task-definition
  revision with the recommendation's `recommended_fargate_size` (`cpu`/`memory`, already snapped to a real
  Fargate size by `snap_to_fargate_size` — never the raw unsnapped `recommended_cpu_vcpu`/`_mem_gib`), then
  `update_service` to point the baseline ECS service at the new revision. No traffic-weight change — this
  never touches the ALB listener rule, matching invariant 5's "traffic shifting is orthogonal to
  right-sizing."

Both exposed as `POST /internal/pipeline-runs/{run_id}/apply-rightsizing` on `pipeline-worker` (or
`policy-controller`, wherever the other `/internal/...` actuation endpoints already live for that deploy
target — check `graduate_canary`'s router registration and mirror it exactly rather than picking a new
convention).

### 2. OPA gate call — mirror `handle_approval`'s exact shape

New `services/policy-controller/src/controller.py::handle_apply_rightsizing(pipeline_run_id, rec,
approver_role, approver_user, ...)`, structurally identical to the existing `handle_approval()`:

```python
approved_signatures = [{"role": approver_role, "user": approver_user}]
opa_input = {
    "requested_action": "APPLY_RIGHTSIZING",
    "rightsizing_recommendation": rec,     # the exact stored cost_analysis.rightsizing_rec row
    "approved_signatures": approved_signatures,
}
```

`require_role("platform-admin")` (already exists in `rbac.py`) gates the api-gateway route before this is
even called — the OPA check is defense in depth, not the only check, matching this platform's existing
"every actuation is gated twice" pattern (RBAC at the API boundary, OPA at the actuation boundary).

### 3. api-gateway route

`POST /api/v1/projects/{project_id}/runs/{run_id}/apply-rightsizing`, `Depends(require_role
("platform-admin"))`, reads the latest `cost_analysis.rightsizing_rec` for that run from Postgres (reuse
the query `reports_router.py` already has), 404s with a clear message if there's no recommendation or it's
already `is_overprovisioned: false`, and proxies to policy-controller's internal endpoint — same shape as
`approve_project_run` (`projects_router.py:3158`) already does for canary-step approval.

### 4. Audit trail

Write an `audit_ledger` row the same way every other actuation does (`audit_writer.py`) — action
`APPLY_RIGHTSIZING`, before/after resource values, the approver's identity, the OPA verdict. This is the
one part of this flow that must never be skipped: it's the only record that a running service's actual
resource footprint changed outside a normal deploy.

### 5. Frontend

`CostTab.tsx`: an "Apply this recommendation" button next to the existing badge, visible only to a
`platform-admin`-role session (reuse whatever role-gating pattern `BlueGreenCutoverPanel.tsx` or the
existing approve-run button already uses — don't invent a new one), behind a confirmation dialog stating
the exact before → after CPU/memory numbers (this is a real, billable, resource-changing action — the
confirmation must show real numbers, not a generic "are you sure?"). On success, refresh the cost history
query so the badge flips to "Right-sized" and shows the new footprint.

### 6. Tests

- `services/policy-controller/tests/` — new test mirroring `test_rollout_ramp.py`'s OPA-input-shape
  assertions for `handle_apply_rightsizing`: denies with no signature, denies with a non-platform-admin
  signature, allows with one.
- Extend `test_ecs_rightsizing.py` (or a new `test_apply_rightsizing_ecs.py`) with a fake ECS client
  asserting `register_task_definition` + `update_service` are called with the snapped size, never the raw
  unsnapped numbers.
- `services/pipeline-worker/tests/` (or wherever the K8s internal endpoint lands) — a fake `apps_v1`
  asserting the patched `resources.requests` match the recommendation exactly.
- `policies/tests/guardrails_test.rego` already likely has no case for Rule 8 at all yet — check, and add
  the three cases (no signature / wrong role / platform-admin) at the OPA level too, matching this
  project's existing "every rego rule has both a Python-side and an `opa test` case" convention.
- One live verification pass against the real running stack (same discipline as every other fix in this
  project): trigger it for real against the Kind cluster or a real ECS baseline service, confirm the actual
  resource requests / task definition changed, confirm the audit row landed, confirm a `developer`-role
  token gets a real 403 before OPA is ever reached.

## Open questions to resolve before implementation (not blocking this plan doc)

1. Where do `pipeline-worker` vs. `policy-controller` internal actuation endpoints get placed for a
   non-traffic-shifting resource change like this — check which service currently owns the baseline
   Deployment/task-definition mutation path for each deploy target before adding a third pattern.
2. Whether `recommended_fargate_size` can ever be `None` (no size found ≥ current footprint, per
   `snap_to_fargate_size`'s own comment about "none above 4 vCPU not covered") — the apply endpoint must
   4xx clearly in that case rather than sending a `None` cpu/memory to `register_task_definition`.
3. Whether an in-flight rollout should block Apply (resizing the baseline mid-rollout could confound the
   in-progress canary comparison) — likely yes, gate on `execution_state.status` being terminal, same
   safeguard the manual-rollback endpoint already applies.
