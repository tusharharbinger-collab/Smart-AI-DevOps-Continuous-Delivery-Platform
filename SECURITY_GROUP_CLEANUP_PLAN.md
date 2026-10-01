# Per-Project Security Group Cleanup — Plan

**Status: plan only, nothing in this file has been implemented yet.**

## The real gap found live (2026-10-01)

Deleting every project from the UI (the real async job-based delete flow, `delete_project` →
`_run_project_deletion_job` → `deprovision_ecs_service`) correctly tore down each project's ECS
services, target groups, and ALB listener rule. It never touched the project's own **task security
group** (`{service_name}-sg`, created by `ensure_task_security_group` in
`services/pipeline-worker/src/aws/ecs_onboarding.py` at onboarding time, allowing inbound traffic on
the app's port only from the shared ALB's security group).

Found while manually tearing down the shared ALB afterward: `testing-2-sg` and `test-zyx-sg` were
both still sitting in the account, and their inbound rules (each referencing the ALB's security group
as an allowed source) were the literal reason `DeleteSecurityGroup` on the ALB's own security group
failed with `DependencyViolation` — a security group can't be deleted while another security group's
rule still names it. Had to delete the two orphaned project security groups by hand before the ALB's
own security group would delete.

**Impact today**: zero direct cost (security groups themselves are free) — but a real, accumulating
orphaned-resource leak. Every project ever deleted leaves one more untracked security group behind
forever, and (as just demonstrated) it silently blocks any later attempt to tear down the shared ALB's
own security group too.

## Goal

`deprovision_ecs_service` deletes the project's own task security group as part of normal project
deletion — the same one-click flow already tears down everything else, this closes the last gap.

## Why this needs care (not just one extra `delete_security_group` call)

A security group can't be deleted while anything still references it:
1. **A live ENI still attached to it.** When `delete_service(force=True)` stops the project's Fargate
   tasks, their network interfaces take a few seconds to fully detach — a `delete_security_group`
   call issued immediately afterward can transiently fail with `DependencyViolation` even though the
   tasks are already gone and it will succeed moments later. This is exactly the shape of eventual
   consistency `wait_for_service_stable`/`wait_for_target_group_healthy` already poll around
   elsewhere in this same file.
2. **Another security group's rule still names it** — not expected for a project's own task SG (only
   the shared ALB SG's rule references it, and the ALB SG is intentionally untouched here since it's
   shared across every project), but defensive handling costs nothing.

So this needs a short bounded retry/poll, not a single best-effort try — matching this file's own
established pattern, not inventing a new one.

## Design

### 1. New function in `shared/aws_ecs_actuation.py`

```python
def delete_task_security_group(
    region: str, service_name: str, timeout_seconds: int = 60, poll_interval_seconds: float = 5.0,
) -> dict:
    """
    Deletes the per-project task security group `{service_name}-sg` created by
    `ensure_task_security_group` at onboarding time. Called AFTER the project's ECS services are
    already deleted (deprovision_ecs_service's existing ordering) — their task ENIs need a few seconds
    to fully detach, which is why this polls rather than trying once. Idempotent: a security group
    that's already gone (never onboarded, or already cleaned up) is reported as such, never an error.
    Fail-soft like every other step in deprovision_ecs_service: a security group that still won't
    delete after the timeout (e.g. something unexpected still references it) is logged and returned as
    "could not delete yet" rather than raising — it costs nothing sitting there, and a stuck delete must
    never block the rest of project deletion from completing.
    """
```

Implementation sketch:
- `ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{service_name}-sg"]}, ...])`
  — if none found, return `{"status": "already_deleted"}` immediately (mirrors `delete_stack`'s and
  `delete_ecr_repository`'s own already-gone handling added earlier this session).
- Otherwise loop up to `timeout_seconds` (default 60s, polling every 5s — same shape as
  `wait_for_target_group_healthy`'s own defaults in this file) calling `delete_security_group`,
  catching exactly `DependencyViolation` as "not yet, ENI still detaching" and retrying; any other
  `ClientError` is a real, unexpected failure and should raise immediately rather than retry blindly.
- On success inside the loop: `{"status": "deleted"}`. On exhausting the timeout: log a warning and
  return `{"status": "still_attached", "group_name": ...}` — not an exception, so the caller's
  fail-soft contract holds.

### 2. Wire it into `deprovision_ecs_service` (`ecs_onboarding.py`)

Call `delete_task_security_group` **last**, after the existing services → listener rule → target
groups sequence (the same "tear down in dependency order" logic that function already documents for
why target groups are deleted after the listener rule, not before). Add its result to the function's
existing `deleted` dict (`deleted["security_group"] = result["status"]`) so the caller's response shape
gains one more field without breaking anything that reads the existing ones.

### 3. Surface it in the project-deletion job's step list (api-gateway)

`_run_project_deletion_job` already shows a `"cluster"` step ("Stopping AWS ECS services and load
balancer routing") whose `detail` field is currently just the raw pipeline-worker response status.
No new step is needed — this is naturally part of that same "stop the running service" step, not a
new line item, since the human-facing action ("tear down everything this project's compute used") is
one concept. Just make sure the real new field from `deprovision_ecs_service`'s response makes it into
that step's `detail` text (e.g. append `"; security group: deleted"` / `"; security group: still
detaching, will not block cost"`) so the progress dialog is honest about this sub-step without needing
a whole new row.

### 4. Tests

- `services/pipeline-worker/tests/test_ecs_onboarding.py` (or wherever `deprovision_ecs_service`'s
  existing tests live) — new cases for `delete_task_security_group`: already-gone is a no-op,
  `DependencyViolation` retries and eventually succeeds, timeout-exhausted returns `still_attached`
  rather than raising, and a non-`DependencyViolation` `ClientError` propagates immediately (never
  silently retried as if it were the ENI-detaching case).
- Extend whatever test already covers `deprovision_ecs_service`'s full `deleted` dict shape to assert
  the new `security_group` key is present and reflects the fake client's behavior.
- One live verification pass against the real running stack (same discipline as every other fix this
  session): onboard a throwaway project, delete it through the real UI delete flow, and confirm via a
  direct `describe_security_groups` call that `{service}-sg` is actually gone afterward — not just that
  the API response claims it is.

## Explicitly out of scope for this plan

- **The shared ALB's own security group** (`smartcd-platform-alb-sg`) is never touched by per-project
  deletion — it's shared infrastructure, and tearing it down is a distinct, much bigger action (as
  demonstrated manually this session) that only makes sense when deprovisioning the whole platform,
  not one project.
- **Retroactively cleaning up security groups from projects already deleted before this fix ships** —
  a one-off manual pass (as already done for `testing-2-sg`/`test-zyx-sg`) is sufficient; this plan is
  about not leaking any more of them going forward, not writing a backfill migration for a handful of
  already-orphaned groups.
