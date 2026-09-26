"""Backlog #4 - fetch_failure_events: real stack events, early-validation detail, never raises."""
from datetime import datetime, timezone

from shared.provisioning import aws_cloudformation as cf

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
GENERIC = "Validation failed with 1 error(s). Call DescribeEvents to retrieve the full list of issues."


class Cfn:
    def __init__(self, events, detail=None, has_describe=True):
        self._events, self._detail = events, detail
        if has_describe:
            self.describe_events = self._describe

    def describe_stack_events(self, StackName):
        return {"StackEvents": self._events}

    def _describe(self, StackName, Filters):
        if isinstance(self._detail, Exception):
            raise self._detail
        return {"OperationEvents": self._detail or []}


def ev(res, status, reason):
    return {"LogicalResourceId": res, "ResourceType": "T", "ResourceStatus": status, "ResourceStatusReason": reason, "Timestamp": TS}


def patch(monkeypatch, cfn):
    monkeypatch.setattr(cf, "aws_client", lambda *a, **k: cfn)


def test_plain_events_are_normalized(monkeypatch):
    patch(monkeypatch, Cfn([ev("Db", "CREATE_FAILED", "boom")]))
    out = cf.fetch_failure_events("s", "us-east-1")
    assert out == [{"resource": "Db", "type": "T", "status": "CREATE_FAILED", "reason": "boom", "timestamp": TS.isoformat()}]


def test_early_validation_detail_replaces_the_generic_pointer(monkeypatch):
    detail = [
        {"LogicalResourceId": "Bucket", "ResourceType": "AWS::S3::Bucket", "ValidationStatusReason": "already exists",
         "ValidationPath": "/Resources/Bucket", "Timestamp": TS},
        {"LogicalResourceId": "S", "ResourceStatusReason": GENERIC, "Timestamp": TS},
    ]
    patch(monkeypatch, Cfn([ev("S", "CREATE_FAILED", GENERIC)], detail))
    out = cf.fetch_failure_events("s", "us-east-1")
    assert [e["reason"] for e in out] == ["already exists (at /Resources/Bucket)"]


def test_old_client_keeps_generic_reason(monkeypatch):
    patch(monkeypatch, Cfn([ev("S", "CREATE_FAILED", GENERIC)], has_describe=False))
    assert cf.fetch_failure_events("s", "us-east-1")[0]["reason"] == GENERIC


def test_detail_call_failure_keeps_generic_reason(monkeypatch):
    patch(monkeypatch, Cfn([ev("S", "CREATE_FAILED", GENERIC)], RuntimeError("denied")))
    assert cf.fetch_failure_events("s", "us-east-1")[0]["reason"] == GENERIC


def test_missing_stack_returns_empty_not_raise(monkeypatch):
    class Bad:
        def describe_stack_events(self, StackName):
            raise RuntimeError("does not exist")

    patch(monkeypatch, Bad())
    assert cf.fetch_failure_events("s", "us-east-1") == []
