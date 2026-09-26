"""
Backlog #3 - shared/provisioning/aws_session.py (bring-your-own-AWS-account via sts:AssumeRole).
STS is faked; this covers OUR security-relevant behaviour: the ExternalId is always sent, sessions are short,
credentials are cached only until near expiry, malformed ARNs never reach STS, and failures don't leak.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from botocore.exceptions import ClientError

from shared.provisioning import aws_session
from shared.provisioning.aws_session import ConnectionError_, client, parse_role_arn, verify_connection

ROLE = "arn:aws:iam::123456789012:role/smartcd-platform-access"
EXT = "ext-id-abc"


class FakeSts:
    def __init__(self, account="123456789012", fail_code=None, minutes=15):
        self.account, self.fail_code, self.minutes = account, fail_code, minutes
        self.assume_calls = []

    def assume_role(self, **kw):
        self.assume_calls.append(kw)
        if self.fail_code:
            raise ClientError({"Error": {"Code": self.fail_code, "Message": "not authorized to perform sts:AssumeRole"}}, "AssumeRole")
        return {"Credentials": {
            "AccessKeyId": "AKIATEMP", "SecretAccessKey": "secret", "SessionToken": "token",
            "Expiration": datetime.now(timezone.utc) + timedelta(minutes=self.minutes),
        }}

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/smartcd-platform-access/smartcd-platform"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    aws_session._clear_cache_for_tests()
    yield
    aws_session._clear_cache_for_tests()


@pytest.fixture
def boto(monkeypatch):
    """Records every boto3.client(...) call; `sts` is the shared FakeSts."""
    sts = FakeSts()
    calls = []

    def fake_client(service, **kw):
        calls.append((service, kw))
        return sts if service == "sts" else ("client", service, kw)

    monkeypatch.setattr(aws_session.boto3, "client", fake_client)
    return sts, calls


# ─────────────── ARN validation ───────────────


@pytest.mark.parametrize("arn", [
    "arn:aws:iam::123456789012:role/smartcd-platform-access",
    "arn:aws:iam::123456789012:role/path/to/role",
    "arn:aws-cn:iam::123456789012:role/r",
    "arn:aws-us-gov:iam::123456789012:role/r",
])
def test_valid_role_arns_yield_their_account_id(arn):
    assert parse_role_arn(arn) == "123456789012"


@pytest.mark.parametrize("arn", [
    "", None, "smartcd-platform-access", "arn:aws:iam::123:role/r",           # too short an account id
    "arn:aws:iam::123456789012:user/bob", "arn:aws:s3:::bucket",              # not a role
    "arn:aws:iam::123456789012:role/", "arn:aws:iam::12345678901a:role/r",
    "arn:aws:iam::123456789012:role/r name", "arn:aws:iam::123456789012:role/r;rm -rf /",
])
def test_malformed_or_non_role_arns_are_rejected_before_anything_reaches_sts(arn):
    with pytest.raises(ValueError):
        parse_role_arn(arn)


def test_verify_rejects_a_bad_arn_without_calling_sts(boto):
    sts, calls = boto
    with pytest.raises(ValueError):
        verify_connection("not-an-arn", EXT)
    assert calls == [] and sts.assume_calls == []


# ─────────────── client() ───────────────


def test_no_connection_means_the_platforms_own_credentials(boto):
    sts, calls = boto
    client("cloudformation", "us-east-1", None)
    assert calls == [("cloudformation", {"region_name": "us-east-1"})]  # no injected credentials
    assert sts.assume_calls == []


def test_a_connection_assumes_the_role_with_the_external_id_and_a_short_session(boto):
    sts, calls = boto
    out = client("rds", "eu-north-1", {"role_arn": ROLE, "external_id": EXT})

    assert sts.assume_calls == [{"RoleArn": ROLE, "RoleSessionName": "smartcd-platform", "ExternalId": EXT, "DurationSeconds": 900}]
    service, kw = calls[-1]
    assert service == "rds" and kw["region_name"] == "eu-north-1"
    assert (kw["aws_access_key_id"], kw["aws_secret_access_key"], kw["aws_session_token"]) == ("AKIATEMP", "secret", "token")
    assert out[0] == "client"


def test_credentials_are_reused_until_shortly_before_they_expire(boto):
    sts, _ = boto
    conn = {"role_arn": ROLE, "external_id": EXT}
    client("rds", "us-east-1", conn)
    client("ecs", "us-east-1", conn)
    assert len(sts.assume_calls) == 1


def test_credentials_close_to_expiry_are_refreshed_not_reused(boto, monkeypatch):
    sts, _ = boto
    sts.minutes = 0.5  # 30s left: inside the 60s refresh margin
    conn = {"role_arn": ROLE, "external_id": EXT}
    client("rds", "us-east-1", conn)
    client("rds", "us-east-1", conn)
    assert len(sts.assume_calls) == 2


def test_the_cache_is_keyed_on_role_and_external_id_so_one_tenants_credentials_never_serve_another(boto):
    sts, _ = boto
    client("rds", "us-east-1", {"role_arn": ROLE, "external_id": "tenant-a"})
    client("rds", "us-east-1", {"role_arn": ROLE, "external_id": "tenant-b"})
    assert [c["ExternalId"] for c in sts.assume_calls] == ["tenant-a", "tenant-b"]


# ─────────────── failures ───────────────


def test_a_refused_assume_role_becomes_a_clear_error_that_does_not_echo_awss_detail(monkeypatch):
    sts = FakeSts(fail_code="AccessDenied")
    monkeypatch.setattr(aws_session.boto3, "client", lambda service, **kw: sts)
    with pytest.raises(ConnectionError_) as e:
        client("rds", "us-east-1", {"role_arn": ROLE, "external_id": EXT})
    msg = str(e.value)
    assert "AccessDenied" in msg and "ExternalId" in msg and ROLE in msg
    assert "not authorized to perform" not in msg  # AWS's own message is not passed through


def test_a_failed_assume_is_not_cached(monkeypatch):
    sts = FakeSts(fail_code="AccessDenied")
    monkeypatch.setattr(aws_session.boto3, "client", lambda service, **kw: sts)
    for _ in range(2):
        with pytest.raises(ConnectionError_):
            client("rds", "us-east-1", {"role_arn": ROLE, "external_id": EXT})
    assert len(sts.assume_calls) == 2


# ─────────────── verify_connection ───────────────


def test_verify_returns_the_customers_account_and_assumed_identity(boto):
    result = verify_connection(ROLE, EXT)
    assert result["account_id"] == "123456789012" and "assumed-role/smartcd-platform-access" in result["arn"]


def test_verify_rejects_an_assumed_identity_from_a_different_account_than_the_arn_names(monkeypatch):
    sts = FakeSts(account="999999999999")
    monkeypatch.setattr(aws_session.boto3, "client", lambda service, **kw: sts)
    with pytest.raises(ConnectionError_, match="999999999999"):
        verify_connection(ROLE, EXT)
