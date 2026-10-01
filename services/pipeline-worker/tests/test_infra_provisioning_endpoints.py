"""
services/pipeline-worker/tests/test_infra_provisioning_endpoints.py

Covers the three new /infra-provisioning/* endpoints (Phase 7,
AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — thin wrappers over
shared/provisioning/aws_cloudformation.py, which has its own direct
boto3-mocked test coverage (test_aws_cloudformation_provisioner.py). These
tests only cover the endpoint's own request/response shaping and error
handling, calling the route handler directly (matching
test_pipeline_validate_endpoint.py's convention — no TestClient/lifespan,
since main.py's lifespan opens real Redis connections).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from fastapi import HTTPException

import src.main as main
from shared.provisioning.base import ChangePreview, ProvisioningResult, ProvisioningStatus, ResourceChange


def test_create_change_set_returns_the_real_changes(monkeypatch):
    monkeypatch.setattr(
        main,
        "cfn_preview_changes",
        lambda template_body, draft_id, region, import_existing=False, stack_name=None, connection=None: ChangePreview(
            change_set_id="cs-1",
            stack_name="smartcd-infra-draft-1",
            stack_id="arn:x",
            status="READY",
            changes=[ResourceChange(action="Add", logical_id="OrdersDb", resource_type="AWS::RDS::DBInstance")],
        ),
    )

    result = asyncio.run(
        main.create_infra_change_set({"template_body": "{}", "draft_id": "draft-1", "region": "us-east-1"})
    )

    assert result["status"] == "READY"
    assert result["changes"][0]["logical_id"] == "OrdersDb"


def test_create_change_set_missing_field_is_422():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.create_infra_change_set({"draft_id": "draft-1"}))
    assert exc_info.value.status_code == 422


def test_create_change_set_provisioner_error_is_502(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError("boto3 exploded")

    monkeypatch.setattr(main, "cfn_preview_changes", _raise)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.create_infra_change_set({"template_body": "{}", "draft_id": "draft-1"}))
    assert exc_info.value.status_code == 502


def test_execute_change_set_returns_status(monkeypatch):
    monkeypatch.setattr(
        main, "cfn_execute_changes", lambda change_set_id, stack_name, region, connection=None: ProvisioningResult(stack_id="arn:x", status="UPDATE_IN_PROGRESS")
    )

    result = asyncio.run(main.execute_infra_change_set({"change_set_id": "cs-1", "stack_name": "smartcd-infra-draft-1"}))

    assert result["status"] == "UPDATE_IN_PROGRESS"


def test_execute_change_set_missing_field_is_422():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.execute_infra_change_set({"change_set_id": "cs-1"}))
    assert exc_info.value.status_code == 422


def test_execute_change_set_provisioner_error_is_502(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError("boto3 exploded")

    monkeypatch.setattr(main, "cfn_execute_changes", _raise)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.execute_infra_change_set({"change_set_id": "cs-1", "stack_name": "x"}))
    assert exc_info.value.status_code == 502


# ───────── delete-stack / delete-image (project-deletion teardown) ─────────


def test_delete_infra_stack_returns_the_real_result(monkeypatch):
    monkeypatch.setattr(
        main, "cfn_delete_stack",
        lambda stack_name, region, connection=None: {"status": "delete_requested", "stack_name": stack_name},
    )

    result = asyncio.run(main.delete_infra_stack({"stack_name": "smartcd-infra-draft-1", "region": "us-east-1"}))

    assert result == {"status": "delete_requested", "stack_name": "smartcd-infra-draft-1"}


def test_delete_infra_stack_missing_field_is_422():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.delete_infra_stack({}))
    assert exc_info.value.status_code == 422


def test_delete_infra_stack_provisioner_error_is_502(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError("boto3 exploded")

    monkeypatch.setattr(main, "cfn_delete_stack", _raise)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.delete_infra_stack({"stack_name": "x"}))
    assert exc_info.value.status_code == 502


def test_delete_service_image_returns_the_real_result(monkeypatch):
    monkeypatch.setattr(
        main, "delete_ecr_repository",
        lambda image_name, region: {"status": "deleted", "repository": "testing-2"},
    )

    result = asyncio.run(main.delete_service_image({
        "image_name": "236087863083.dkr.ecr.us-east-1.amazonaws.com/testing-2", "region": "us-east-1",
    }))

    assert result == {"status": "deleted", "repository": "testing-2"}


def test_delete_service_image_missing_field_is_422():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.delete_service_image({}))
    assert exc_info.value.status_code == 422


def test_delete_service_image_ecr_error_is_502(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError("boto3 exploded")

    monkeypatch.setattr(main, "delete_ecr_repository", _raise)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.delete_service_image({"image_name": "x"}))
    assert exc_info.value.status_code == 502


def test_get_status_returns_real_outputs(monkeypatch):
    monkeypatch.setattr(
        main,
        "cfn_check_status",
        lambda stack_name, region, connection=None: ProvisioningStatus(
            status="UPDATE_COMPLETE", is_terminal=True, succeeded=True, outputs={"DbEndpoint": "db.example.com"}
        ),
    )
    monkeypatch.setattr(main, "cfn_fetch_failure_events", lambda stack_name, region, connection=None: [])

    result = asyncio.run(main.get_infra_provisioning_status("smartcd-infra-draft-1", "us-east-1"))

    assert result["succeeded"] is True
    assert result["outputs"]["DbEndpoint"] == "db.example.com"


def test_get_status_includes_real_per_resource_events(monkeypatch):
    # AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7.4.1 - the live build view (Phase F, Stage C) needs
    # real per-resource events during an IN-PROGRESS poll, not just after a terminal failure.
    monkeypatch.setattr(
        main, "cfn_check_status",
        lambda stack_name, region, connection=None: ProvisioningStatus(
            status="CREATE_IN_PROGRESS", is_terminal=False, succeeded=False, outputs={}
        ),
    )
    events = [
        {"resource": "OrdersDb", "type": "AWS::RDS::DBInstance", "status": "CREATE_IN_PROGRESS", "reason": None, "timestamp": "2026-09-29T00:00:00"},
        {"resource": "CacheCluster", "type": "AWS::ElastiCache::CacheCluster", "status": "CREATE_COMPLETE", "reason": None, "timestamp": "2026-09-29T00:00:01"},
    ]
    monkeypatch.setattr(main, "cfn_fetch_failure_events", lambda stack_name, region, connection=None: events)

    result = asyncio.run(main.get_infra_provisioning_status("smartcd-infra-draft-1", "us-east-1"))

    assert result["resource_events"] == events


def test_create_change_set_threads_import_and_stack_name_to_the_provisioner(monkeypatch):
    # AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md: source='existing' drafts import, and an edit of a
    # provisioned draft must UPDATE the parent's stack instead of creating a colliding new one.
    seen = {}

    def fake(template_body, draft_id, region, import_existing=False, stack_name=None, connection=None):
        seen.update(import_existing=import_existing, stack_name=stack_name, connection=connection)
        return ChangePreview(change_set_id="cs-1", stack_name="s", stack_id="a", status="READY")

    monkeypatch.setattr(main, "cfn_preview_changes", fake)
    asyncio.run(main.create_infra_change_set({
        "template_body": "{}", "draft_id": "d", "import_existing": True, "stack_name": "smartcd-infra-parent"}))
    assert seen == {"import_existing": True, "stack_name": "smartcd-infra-parent", "connection": None}

    asyncio.run(main.create_infra_change_set({"template_body": "{}", "draft_id": "d"}))
    assert seen == {"import_existing": False, "stack_name": None, "connection": None}


def test_describe_existing_maps_an_unknown_resource_to_422(monkeypatch):
    def boom(selection, archetype, region, connection=None):
        raise ValueError("No existing database named 'ghost' found in us-east-1.")

    monkeypatch.setattr(main, "aws_describe_selected", boom)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.describe_existing_infra({"selection": {"database": "ghost"}, "archetype": "web_service_with_database"}))
    assert exc_info.value.status_code == 422 and "ghost" in exc_info.value.detail


def test_a_tenants_connection_is_forwarded_to_the_provisioner(monkeypatch):
    seen = {}

    def fake(template_body, draft_id, region, import_existing=False, stack_name=None, connection=None):
        seen["connection"] = connection
        return ChangePreview(change_set_id="cs-1", stack_name="s", stack_id="a", status="READY")

    monkeypatch.setattr(main, "cfn_preview_changes", fake)
    conn = {"role_arn": "arn:aws:iam::123456789012:role/smartcd-platform-access", "external_id": "x" * 43}
    asyncio.run(main.create_infra_change_set({"template_body": "{}", "draft_id": "d", "connection": conn, "extra": "ignored"}))
    assert seen["connection"] == conn


@pytest.mark.parametrize("bad", [{"role_arn": "arn:x"}, {"external_id": "e"}, "a string", ["list"]])
def test_a_malformed_connection_is_rejected_not_silently_treated_as_the_platform_account(bad):
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.create_infra_change_set({"template_body": "{}", "draft_id": "d", "connection": bad}))
    assert e.value.status_code == 422


def test_the_status_post_carries_the_connection_in_the_body_never_the_url(monkeypatch):
    seen = {}

    def fake_status(stack_name, region, connection=None):
        seen.update(stack=stack_name, connection=connection)
        return ProvisioningStatus(status="CREATE_COMPLETE", is_terminal=True, succeeded=True, outputs={}, status_reason=None)

    monkeypatch.setattr(main, "cfn_check_status", fake_status)
    conn = {"role_arn": "arn:aws:iam::123456789012:role/r", "external_id": "e" * 43}
    out = asyncio.run(main.post_infra_provisioning_status({"stack_name": "s1", "connection": conn}))
    assert out["status"] == "CREATE_COMPLETE" and seen == {"stack": "s1", "connection": conn}


def test_verify_maps_refusals_to_422_with_the_reason(monkeypatch):
    from shared.provisioning.aws_session import ConnectionError_

    def refuse(role_arn, external_id):
        raise ConnectionError_("Could not assume the role (AccessDenied)")

    monkeypatch.setattr(main, "aws_verify_connection", refuse)
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.verify_aws_connection({"role_arn": "arn:aws:iam::123456789012:role/r", "external_id": "e"}))
    assert e.value.status_code == 422 and "AccessDenied" in e.value.detail


def test_verify_returns_the_customers_account_on_success(monkeypatch):
    monkeypatch.setattr(main, "aws_verify_connection", lambda r, e: {"account_id": "123456789012", "arn": "arn:x"})
    out = asyncio.run(main.verify_aws_connection({"role_arn": "arn:aws:iam::123456789012:role/r", "external_id": "e"}))
    assert out == {"account_id": "123456789012", "arn": "arn:x"}


def test_discovery_in_a_tenant_account_forwards_the_connection(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "aws_discover_existing", lambda archetype, region, connection=None: seen.update(c=connection, r=region) or {"database": []})
    conn = {"role_arn": "arn:aws:iam::123456789012:role/r", "external_id": "e" * 43}
    out = asyncio.run(main.discover_existing_infra_for_connection({"archetype": "web_service_with_database", "region": "eu-north-1", "connection": conn}))
    assert out == {"database": []} and seen == {"c": conn, "r": "eu-north-1"}


# ───────── platform network context (extras are placed with real ids) ─────────

from shared.provisioning import aws_discovery as _disc


class _Ec2:
    def __init__(self, vpcs, subnets):
        self._v, self._s = vpcs, subnets

    def describe_vpcs(self, Filters):
        return {"Vpcs": self._v}

    def describe_subnets(self, Filters):
        return {"Subnets": self._s}


def test_platform_network_context_picks_one_subnet_per_az(monkeypatch):
    ec2 = _Ec2([{"VpcId": "vpc-1", "CidrBlock": "172.31.0.0/16"}], [
        {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1a"}, {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
        {"SubnetId": "subnet-c", "AvailabilityZone": "us-east-1b"},
    ])
    monkeypatch.setattr(_disc, "aws_client", lambda *a, **k: ec2)
    out = _disc.get_platform_network_context("us-east-1")
    assert out["vpc_id"] == "vpc-1" and out["vpc_cidr"] == "172.31.0.0/16"
    assert out["subnet_ids"] == ["subnet-a", "subnet-c"]


def test_platform_network_context_needs_a_default_vpc_and_two_azs(monkeypatch):
    import pytest
    monkeypatch.setattr(_disc, "aws_client", lambda *a, **k: _Ec2([], []))
    with pytest.raises(RuntimeError):
        _disc.get_platform_network_context("us-east-1")
    one_az = _Ec2([{"VpcId": "v", "CidrBlock": "10.0.0.0/16"}], [{"SubnetId": "s", "AvailabilityZone": "a"}])
    monkeypatch.setattr(_disc, "aws_client", lambda *a, **k: one_az)
    with pytest.raises(RuntimeError):
        _disc.get_platform_network_context("us-east-1")
