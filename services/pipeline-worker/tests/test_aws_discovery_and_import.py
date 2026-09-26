"""
AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phases B/C on the AWS side - boto3 is mocked, so this
covers OUR logic (slot scoping, re-verification, the change-set kwargs), not AWS itself.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from shared.provisioning import aws_cloudformation, aws_discovery


class _FakeRds:
    def describe_db_instances(self):
        return {"DBInstances": [{"DBInstanceIdentifier": "orders-db", "Engine": "postgres", "DBInstanceClass": "db.t3.micro", "MultiAZ": False}]}


class _FakeEcs:
    def list_clusters(self):
        return {"clusterArns": ["arn:c1"]}

    def describe_clusters(self, clusters):
        return {"clusters": [{"clusterName": "prod", "status": "ACTIVE"}]}


class _Boom:
    def describe_load_balancers(self):
        raise RuntimeError("access denied")


def _patch_clients(monkeypatch, **clients):
    monkeypatch.setattr(aws_discovery, "aws_client", lambda name, region, connection=None: clients[name])


def test_archetype_scopes_which_slots_are_listed(monkeypatch):
    # web_service_with_database never asks for ElastiCache - 'elasticache' isn't even mocked.
    _patch_clients(monkeypatch, rds=_FakeRds(), ecs=_FakeEcs(), elbv2=_Boom())
    out = aws_discovery.discover_existing("web_service_with_database", "us-east-1")
    assert set(out) == {"ecs_cluster", "load_balancer", "database"}
    assert out["database"][0]["id"] == "orders-db"
    assert out["database"][0]["details"]["engine"] == "postgres"


def test_one_unreadable_service_does_not_hide_the_rest(monkeypatch):
    _patch_clients(monkeypatch, rds=_FakeRds(), ecs=_FakeEcs(), elbv2=_Boom())
    out = aws_discovery.discover_existing("web_service_with_database", "us-east-1")
    assert out["load_balancer"] == []
    assert out["ecs_cluster"][0]["id"] == "prod"


def test_describe_selected_returns_real_details(monkeypatch):
    _patch_clients(monkeypatch, rds=_FakeRds())
    out = aws_discovery.describe_selected({"database": "orders-db"}, "web_service_with_database", "us-east-1")
    assert out == {"database": {"id": "orders-db", "details": out["database"]["details"]}}
    assert out["database"]["details"]["instance_class"] == "db.t3.micro"


def test_describe_selected_rejects_an_identifier_aws_does_not_list(monkeypatch):
    _patch_clients(monkeypatch, rds=_FakeRds())
    with pytest.raises(ValueError, match="ghost"):
        aws_discovery.describe_selected({"database": "ghost"}, "web_service_with_database", "us-east-1")


def test_describe_selected_rejects_a_slot_the_archetype_does_not_use(monkeypatch):
    _patch_clients(monkeypatch, rds=_FakeRds())
    with pytest.raises(ValueError, match="not valid for archetype"):
        aws_discovery.describe_selected({"database": "orders-db"}, "stateless_web_service", "us-east-1")


class _FakeCfn:
    def __init__(self, stack_exists=False):
        self.stack_exists = stack_exists
        self.kwargs = None

    def describe_stacks(self, StackName):
        if not self.stack_exists:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ValidationError", "Message": f"Stack {StackName} does not exist"}}, "DescribeStacks")
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}

    def create_change_set(self, **kwargs):
        self.kwargs = kwargs
        return {"Id": "cs-1"}

    def describe_change_set(self, ChangeSetName, StackName):
        return {"Status": "CREATE_COMPLETE", "StackId": "arn:stack", "Changes": [
            {"ResourceChange": {"Action": "Import", "LogicalResourceId": "OrdersDb", "ResourceType": "AWS::RDS::DBInstance"}}]}


def _cfn(monkeypatch, fake):
    monkeypatch.setattr(aws_cloudformation.boto3, "client", lambda name, region_name=None: fake)


def test_import_existing_sets_the_import_parameter_and_surfaces_import_actions(monkeypatch):
    fake = _FakeCfn()
    _cfn(monkeypatch, fake)
    preview = aws_cloudformation.preview_changes("{}", "draft-1", "us-east-1", import_existing=True)
    assert fake.kwargs["ImportExistingResources"] is True
    assert preview.status == "READY" and preview.changes[0].action == "Import"


def test_ai_created_does_not_send_the_import_parameter(monkeypatch):
    fake = _FakeCfn()
    _cfn(monkeypatch, fake)
    aws_cloudformation.preview_changes("{}", "draft-1", "us-east-1")
    assert "ImportExistingResources" not in fake.kwargs


def test_stack_name_override_makes_an_edit_an_update_of_the_parents_stack(monkeypatch):
    fake = _FakeCfn(stack_exists=True)
    _cfn(monkeypatch, fake)
    aws_cloudformation.preview_changes("{}", "child-draft", "us-east-1", stack_name="smartcd-infra-parent")
    assert fake.kwargs["StackName"] == "smartcd-infra-parent"
    assert fake.kwargs["ChangeSetType"] == "UPDATE"


def test_default_stack_name_is_still_derived_from_the_draft(monkeypatch):
    fake = _FakeCfn()
    _cfn(monkeypatch, fake)
    aws_cloudformation.preview_changes("{}", "draft-1", "us-east-1")
    assert fake.kwargs["StackName"] == "smartcd-infra-draft-1"
