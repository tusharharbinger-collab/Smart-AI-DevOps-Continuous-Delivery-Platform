"""
shared/provisioning/aws_cloudformation.py

Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — the real, only
implementation of `InfraProvisioner` for now. Uses AWS CloudFormation
Change Sets specifically because they give a real, zero-risk preview
(`create_change_set` makes no changes to any resource — it only computes
what WOULD happen) and automatic rollback-on-failure, natively, with no
new state-management infrastructure to build — see that plan's §1 for why
this was chosen over running raw Terraform or hand-writing a hand-rolled
sequence of individual boto3 calls per resource type.

Lives in `shared/` (not pipeline-worker's own src/) for the same reason
`aws_ecs_actuation.py` does: this is safety-critical, real-money-spending
logic, and this codebase already treats that category as needing exactly
one source of truth. Called from pipeline-worker's own endpoints (the
service that already holds AWS credentials and boto3 dependency) via
`asyncio.to_thread`, since boto3 is synchronous — same pattern
`onboard_service_aws_now` already uses.
"""
import re
import time

import boto3
import structlog
from botocore.exceptions import ClientError, WaiterError

from shared.provisioning.aws_session import client as aws_client
from shared.provisioning.base import ChangePreview, ProvisioningResult, ProvisioningStatus, ResourceChange

logger = structlog.get_logger(__name__)

STACK_NAME_PREFIX = "smartcd-infra-"
# CloudFormation stack names: alphanumeric + hyphens, must start with a
# letter, max 128 chars. draft_id is a UUID (36 chars incl. hyphens), so
# prefix + uuid comfortably fits.
_STACK_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9-]")

# create_change_set is async on AWS's side even though the boto3 call
# returns immediately — poll describe_change_set until it leaves
# CREATE_PENDING/CREATE_IN_PROGRESS. Real-world change sets for a handful
# of resources settle in a few seconds; this ceiling is generous, not
# tuned to a measured p99, since a slow preview is still a preview (no
# resources changed yet) and failing it early would only force a retry.
_CHANGE_SET_POLL_TIMEOUT_SECONDS = 60
_CHANGE_SET_POLL_INTERVAL_SECONDS = 2

_TERMINAL_SUCCESS_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_TERMINAL_FAILURE_STATUSES = {
    "CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED", "DELETE_FAILED",
}


def stack_name_for_draft(draft_id: str) -> str:
    """Deterministic, idempotent: the same draft always maps to the same stack name,
    so re-previewing an already-provisioned draft correctly computes an UPDATE, not a
    second, colliding CREATE."""
    safe = _STACK_NAME_SAFE_RE.sub("-", draft_id)
    return f"{STACK_NAME_PREFIX}{safe}"[:128]


def _stack_exists(cfn, stack_name: str) -> bool:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        # A stack left over from a failed CREATE is not a real existing stack
        # to UPDATE against — CloudFormation requires deleting it first.
        return resp["Stacks"][0]["StackStatus"] not in ("REVIEW_IN_PROGRESS", "ROLLBACK_COMPLETE")
    except ClientError as e:
        if "does not exist" in str(e):
            return False
        raise


def preview_changes(
    template_body: str, draft_id: str, region: str, import_existing: bool = False, stack_name: str | None = None,
    connection: dict | None = None,
) -> ChangePreview:
    """
    Real AWS API calls, zero risk — `create_change_set` never modifies any
    resource, it only computes a diff. This is the artifact a human reviews
    before anything real happens (§3 of the plan's two-approval design).
    """
    cfn = aws_client("cloudformation", region, connection)
    # An edited draft of an already-provisioned one inherits its parent's stack (AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md
    # 2.3) so the change set is an UPDATE of the SAME stack, not a second colliding CREATE.
    stack_name = stack_name or stack_name_for_draft(draft_id)
    change_set_name = f"cs-{int(time.time())}"
    change_set_type = "UPDATE" if _stack_exists(cfn, stack_name) else "CREATE"

    create_kwargs = dict(
        StackName=stack_name,
        TemplateBody=template_body,
        ChangeSetName=change_set_name,
        ChangeSetType=change_set_type,
        Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
    )
    if import_existing:
        # AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md §1.3: a draft with source='existing' has some
        # resources that already exist (each declared DeletionPolicy: Retain, enforced by
        # infra_generator) and possibly some new ones. ImportExistingResources adopts the
        # former and creates the latter in ONE change set — a pure ChangeSetType=IMPORT
        # change set cannot create anything new, so it would break the mixed case.
        create_kwargs["ImportExistingResources"] = True

    try:
        create_resp = cfn.create_change_set(**create_kwargs)
    except ClientError as e:
        logger.error("cfn_create_change_set_request_failed", stack_name=stack_name, error=str(e))
        return ChangePreview(
            change_set_id="", stack_name=stack_name, stack_id=None,
            status="FAILED", status_reason=str(e),
        )

    change_set_id = create_resp["Id"]
    deadline = time.monotonic() + _CHANGE_SET_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        desc = cfn.describe_change_set(ChangeSetName=change_set_id, StackName=stack_name)
        status = desc["Status"]
        if status not in ("CREATE_PENDING", "CREATE_IN_PROGRESS"):
            break
        time.sleep(_CHANGE_SET_POLL_INTERVAL_SECONDS)
    else:
        logger.warning("cfn_change_set_creation_timed_out", stack_name=stack_name, change_set_id=change_set_id)
        return ChangePreview(
            change_set_id=change_set_id, stack_name=stack_name, stack_id=None,
            status="FAILED", status_reason="Timed out waiting for the change set to compute.",
        )

    if status == "FAILED":
        reason = desc.get("StatusReason", "")
        # A change set that fails ONLY because there is nothing to change is
        # not a real failure — it means the draft's template already matches
        # what exists. Surface it as its own status rather than "FAILED" so
        # the UI doesn't show a red error for a no-op.
        if "didn't contain changes" in reason or "No updates are to be performed" in reason:
            return ChangePreview(
                change_set_id=change_set_id, stack_name=stack_name,
                stack_id=desc.get("StackId"), status="NO_CHANGES", status_reason=reason,
            )
        logger.warning("cfn_change_set_failed", stack_name=stack_name, reason=reason)
        return ChangePreview(
            change_set_id=change_set_id, stack_name=stack_name,
            stack_id=desc.get("StackId"), status="FAILED", status_reason=reason,
        )

    changes = [
        ResourceChange(
            action=c["ResourceChange"]["Action"],
            logical_id=c["ResourceChange"]["LogicalResourceId"],
            resource_type=c["ResourceChange"]["ResourceType"],
        )
        for c in desc.get("Changes", [])
    ]
    return ChangePreview(
        change_set_id=change_set_id, stack_name=stack_name,
        stack_id=desc.get("StackId"), status="READY", changes=changes,
    )


def execute_changes(change_set_id: str, stack_name: str, region: str, connection: dict | None = None) -> ProvisioningResult:
    """Only ever called after a human has seen the real preview from `preview_changes` —
    this is the one call in this module that actually creates/modifies real AWS resources."""
    cfn = aws_client("cloudformation", region, connection)
    cfn.execute_change_set(ChangeSetName=change_set_id, StackName=stack_name)
    desc = cfn.describe_stacks(StackName=stack_name)
    stack = desc["Stacks"][0]
    logger.info("cfn_execute_change_set_started", stack_name=stack_name, change_set_id=change_set_id)
    return ProvisioningResult(stack_id=stack["StackId"], status=stack["StackStatus"])


def check_status(stack_name: str, region: str, connection: dict | None = None) -> ProvisioningStatus:
    """Polled by the frontend (via api-gateway) after execute_changes — CloudFormation
    provisioning is asynchronous and can take anywhere from seconds to several minutes."""
    cfn = aws_client("cloudformation", region, connection)
    try:
        desc = cfn.describe_stacks(StackName=stack_name)
    except ClientError as e:
        return ProvisioningStatus(status="NOT_FOUND", is_terminal=True, succeeded=False, status_reason=str(e))

    stack = desc["Stacks"][0]
    status = stack["StackStatus"]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    is_terminal = status in _TERMINAL_SUCCESS_STATUSES or status in _TERMINAL_FAILURE_STATUSES
    return ProvisioningStatus(
        status=status,
        is_terminal=is_terminal,
        succeeded=status in _TERMINAL_SUCCESS_STATUSES,
        outputs=outputs,
        status_reason=stack.get("StackStatusReason"),
    )


def delete_stack(stack_name: str, region: str, connection: dict | None = None) -> dict:
    """
    Real gap found live: an AI-provisioned draft's real resources (an S3 bucket, a database, …) had no
    teardown path anywhere in the platform — deleting the PROJECT that referenced the draft never touched
    the draft's own CloudFormation stack, so its resources (and their cost) outlived the project forever.
    Fire-and-forget like `execute_changes`: CloudFormation deletion is asynchronous on AWS's side regardless
    of whether this call waits, so this only issues the request. Idempotent — a stack that's already gone,
    already being deleted, or never existed is reported as such rather than raised as an error, matching
    every other best-effort teardown call in this codebase.
    """
    cfn = aws_client("cloudformation", region, connection)
    try:
        desc = cfn.describe_stacks(StackName=stack_name)
    except ClientError as e:
        if "does not exist" in str(e):
            return {"status": "already_deleted", "stack_name": stack_name}
        raise
    current_status = desc["Stacks"][0]["StackStatus"]
    if current_status == "DELETE_IN_PROGRESS":
        return {"status": "delete_in_progress", "stack_name": stack_name}
    if current_status == "DELETE_COMPLETE":
        return {"status": "already_deleted", "stack_name": stack_name}
    cfn.delete_stack(StackName=stack_name)
    logger.info("cfn_delete_stack_requested", stack_name=stack_name)
    return {"status": "delete_requested", "stack_name": stack_name}


def fetch_failure_events(stack_name: str, region: str, connection: dict | None = None, limit: int = 40) -> list[dict]:
    """
    Real CloudFormation stack events for the failure analyst (backlog #4). Returns only the fields the analysis
    needs, newest-window first capped at `limit`. Never raises: a missing stack or denied permission yields [] so a
    failure explanation degrades to the status reason rather than failing.
    """
    try:
        cfn = aws_client("cloudformation", region, connection)
        resp = cfn.describe_stack_events(StackName=stack_name)
    except Exception as e:
        logger.warning("cfn_describe_stack_events_failed", stack_name=stack_name, error=str(e))
        return []
    events = []
    for e in resp.get("StackEvents", [])[:limit]:
        ts = e.get("Timestamp")
        events.append({
            "resource": e.get("LogicalResourceId"),
            "type": e.get("ResourceType"),
            "status": e.get("ResourceStatus"),
            "reason": e.get("ResourceStatusReason"),
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
        })
    # CloudFormation's early validation reports only "Call DescribeEvents ..." on the stack event; the real
    # resource/property-level cause lives there. Older boto3 clients lack the call - keep the generic reason then.
    if any("DescribeEvents" in (ev["reason"] or "") for ev in events) and hasattr(cfn, "describe_events"):
        try:
            detail = cfn.describe_events(StackName=stack_name, Filters={"FailedEvents": True})
        except Exception as e:
            logger.warning("cfn_describe_events_failed", stack_name=stack_name, error=str(e))
            detail = {}
        found = []
        for d in detail.get("OperationEvents", [])[:limit]:
            reason = d.get("ValidationStatusReason") or d.get("ResourceStatusReason") or d.get("HookStatusReason")
            if not reason or "DescribeEvents" in reason:
                continue
            if d.get("ValidationPath"):
                reason = f"{reason} (at {d['ValidationPath']})"
            ts = d.get("Timestamp")
            found.append({
                "resource": d.get("LogicalResourceId"),
                "type": d.get("ResourceType"),
                "status": "CREATE_FAILED",
                "reason": reason,
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
            })
        if found:
            events = [ev for ev in events if "DescribeEvents" not in (ev["reason"] or "")] + found
    return events
