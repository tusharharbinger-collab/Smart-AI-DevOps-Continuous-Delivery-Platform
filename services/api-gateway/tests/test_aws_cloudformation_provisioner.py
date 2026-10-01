"""
services/api-gateway/tests/test_aws_cloudformation_provisioner.py

Covers shared/provisioning/aws_cloudformation.py — Phase 7's real
provisioning execution layer. Fake boto3 client only, matching this repo's
established pattern for AWS tests (test_ecs_onboarding.py) — no real AWS
calls. `time.sleep` is monkeypatched to a no-op so the change-set polling
loop doesn't actually slow the test suite down.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from botocore.exceptions import ClientError

import shared.provisioning.aws_cloudformation as cfn_module
from shared.provisioning.aws_cloudformation import (
    check_status,
    delete_stack,
    execute_changes,
    preview_changes,
    stack_name_for_draft,
)


def test_stack_name_is_deterministic_and_sanitized():
    name1 = stack_name_for_draft("abc-123-def")
    name2 = stack_name_for_draft("abc-123-def")
    assert name1 == name2
    assert name1.startswith("smartcd-infra-")
    assert len(name1) <= 128


class _FakeCfnClient:
    def __init__(
        self,
        stack_exists=False,
        change_set_statuses=None,
        change_set_changes=None,
        create_change_set_error=None,
        stack_status="UPDATE_COMPLETE",
    ):
        self._stack_exists = stack_exists
        # A list consumed one call at a time by describe_change_set, so a
        # test can simulate CREATE_PENDING -> CREATE_IN_PROGRESS -> CREATE_COMPLETE.
        self._change_set_statuses = list(change_set_statuses or ["CREATE_COMPLETE"])
        self._change_set_changes = change_set_changes or []
        self._create_change_set_error = create_change_set_error
        self._stack_status = stack_status
        self.create_change_set_calls = []
        self.execute_change_set_calls = []
        self.delete_stack_calls = []

    def describe_stacks(self, StackName):
        if not self._stack_exists:
            raise ClientError(
                {"Error": {"Code": "ValidationError", "Message": f"Stack with id {StackName} does not exist"}},
                "DescribeStacks",
            )
        return {
            "Stacks": [
                {
                    "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/x/abc",
                    "StackStatus": self._stack_status,
                    "StackStatusReason": None,
                    "Outputs": [{"OutputKey": "DbEndpoint", "OutputValue": "db.example.com"}],
                }
            ]
        }

    def delete_stack(self, StackName):
        self.delete_stack_calls.append(StackName)

    def create_change_set(self, **kwargs):
        if self._create_change_set_error:
            raise self._create_change_set_error
        self.create_change_set_calls.append(kwargs)
        return {"Id": "cs-arn-123"}

    def describe_change_set(self, ChangeSetName, StackName):
        status = self._change_set_statuses.pop(0) if len(self._change_set_statuses) > 1 else self._change_set_statuses[0]
        result = {"Status": status, "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/x/abc"}
        if status == "CREATE_COMPLETE":
            result["Changes"] = self._change_set_changes
        elif status == "FAILED":
            result["StatusReason"] = getattr(self, "_failure_reason", "Some real failure reason")
        return result

    def execute_change_set(self, ChangeSetName, StackName):
        self.execute_change_set_calls.append((ChangeSetName, StackName))


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(cfn_module.boto3, "client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(cfn_module.time, "sleep", lambda *_: None)


def test_preview_changes_new_stack_uses_create_type(monkeypatch):
    fake = _FakeCfnClient(stack_exists=False)
    _patch_client(monkeypatch, fake)

    result = preview_changes('{"Resources": {}}', "draft-1", "us-east-1")

    assert fake.create_change_set_calls[0]["ChangeSetType"] == "CREATE"
    assert result.status == "READY"


def test_preview_changes_existing_stack_uses_update_type(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True)
    _patch_client(monkeypatch, fake)

    preview_changes('{"Resources": {}}', "draft-1", "us-east-1")

    assert fake.create_change_set_calls[0]["ChangeSetType"] == "UPDATE"


def test_preview_changes_returns_real_resource_changes(monkeypatch):
    fake = _FakeCfnClient(
        stack_exists=False,
        change_set_changes=[
            {
                "ResourceChange": {
                    "Action": "Add",
                    "LogicalResourceId": "OrdersDb",
                    "ResourceType": "AWS::RDS::DBInstance",
                }
            }
        ],
    )
    _patch_client(monkeypatch, fake)

    result = preview_changes('{"Resources": {}}', "draft-1", "us-east-1")

    assert result.status == "READY"
    assert len(result.changes) == 1
    assert result.changes[0].action == "Add"
    assert result.changes[0].logical_id == "OrdersDb"
    assert result.changes[0].resource_type == "AWS::RDS::DBInstance"


def test_preview_changes_polls_through_in_progress_states(monkeypatch):
    fake = _FakeCfnClient(stack_exists=False, change_set_statuses=["CREATE_PENDING", "CREATE_IN_PROGRESS", "CREATE_COMPLETE"])
    _patch_client(monkeypatch, fake)

    result = preview_changes('{"Resources": {}}', "draft-1", "us-east-1")

    assert result.status == "READY"


def test_preview_changes_no_op_is_reported_as_no_changes_not_failed(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True, change_set_statuses=["FAILED"])
    fake._failure_reason = "The submitted information didn't contain changes."
    _patch_client(monkeypatch, fake)

    result = preview_changes('{"Resources": {}}', "draft-1", "us-east-1")

    assert result.status == "NO_CHANGES"


def test_preview_changes_a_real_failure_is_reported_as_failed(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True, change_set_statuses=["FAILED"])
    fake._failure_reason = "Template contains errors: circular dependency."
    _patch_client(monkeypatch, fake)

    result = preview_changes('{"Resources": {}}', "draft-1", "us-east-1")

    assert result.status == "FAILED"
    assert "circular dependency" in result.status_reason


def test_preview_changes_request_error_is_reported_as_failed(monkeypatch):
    fake = _FakeCfnClient(
        create_change_set_error=ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Template format error"}}, "CreateChangeSet"
        )
    )
    _patch_client(monkeypatch, fake)

    result = preview_changes("not valid json at all", "draft-1", "us-east-1")

    assert result.status == "FAILED"
    assert "Template format error" in result.status_reason


def test_execute_changes_calls_real_execute_and_returns_status(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True)
    _patch_client(monkeypatch, fake)

    result = execute_changes("cs-arn-123", "smartcd-infra-draft-1", "us-east-1")

    assert fake.execute_change_set_calls == [("cs-arn-123", "smartcd-infra-draft-1")]
    assert result.status == "UPDATE_COMPLETE"


def test_check_status_reports_success_and_outputs(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True)
    _patch_client(monkeypatch, fake)

    result = check_status("smartcd-infra-draft-1", "us-east-1")

    assert result.status == "UPDATE_COMPLETE"
    assert result.is_terminal is True
    assert result.succeeded is True
    assert result.outputs == {"DbEndpoint": "db.example.com"}


def test_check_status_not_found_is_terminal_and_failed(monkeypatch):
    fake = _FakeCfnClient(stack_exists=False)
    _patch_client(monkeypatch, fake)

    result = check_status("smartcd-infra-nonexistent", "us-east-1")

    assert result.is_terminal is True
    assert result.succeeded is False
    assert result.status == "NOT_FOUND"


# ───────── delete_stack (project-deletion teardown) ─────────


def test_delete_stack_on_an_existing_stack_issues_the_real_delete(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True, stack_status="CREATE_COMPLETE")
    _patch_client(monkeypatch, fake)

    result = delete_stack("smartcd-infra-draft-1", "us-east-1")

    assert fake.delete_stack_calls == ["smartcd-infra-draft-1"]
    assert result == {"status": "delete_requested", "stack_name": "smartcd-infra-draft-1"}


def test_delete_stack_on_a_missing_stack_is_a_no_op_not_an_error(monkeypatch):
    # A project whose linked infra draft never got past INFRA_PENDING_APPROVAL has no real stack at all -
    # this must be reported as already-deleted, never raised.
    fake = _FakeCfnClient(stack_exists=False)
    _patch_client(monkeypatch, fake)

    result = delete_stack("smartcd-infra-nonexistent", "us-east-1")

    assert result == {"status": "already_deleted", "stack_name": "smartcd-infra-nonexistent"}
    assert fake.delete_stack_calls == []


def test_delete_stack_already_deleting_is_reported_not_reissued(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True, stack_status="DELETE_IN_PROGRESS")
    _patch_client(monkeypatch, fake)

    result = delete_stack("smartcd-infra-draft-1", "us-east-1")

    assert result == {"status": "delete_in_progress", "stack_name": "smartcd-infra-draft-1"}
    assert fake.delete_stack_calls == []


def test_delete_stack_already_deleted_is_a_no_op(monkeypatch):
    fake = _FakeCfnClient(stack_exists=True, stack_status="DELETE_COMPLETE")
    _patch_client(monkeypatch, fake)

    result = delete_stack("smartcd-infra-draft-1", "us-east-1")

    assert result == {"status": "already_deleted", "stack_name": "smartcd-infra-draft-1"}
    assert fake.delete_stack_calls == []

