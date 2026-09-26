"""
services/api-gateway/tests/test_aws_connections.py

Backlog #3 - bring-your-own-AWS-account: the connections router, the customer role template, and how a
draft uses a connection. STS/AWS are behind pipeline-worker and faked here; the security properties under
test are ours: platform-generated unguessable ExternalIds, tenant isolation, nothing usable until verified,
no silent fallback to the platform's account, and a role template that is least-privilege.
"""
import asyncio
import json
import subprocess
import sys
import os
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException

from src import aws_connection_template as tpl
from src.routers import aws_connections_router as router_module
from src.routers import projects_router
from src.routers.aws_connections_router import (
    CreateConnectionRequest, VerifyConnectionRequest, create_connection, delete_connection, get_connection,
    list_connections, load_provisioning_connection, verify_connection,
)

PLATFORM = "236087863083"
CONN_ID = "22222222-2222-2222-2222-222222222222"
ROLE = "arn:aws:iam::123456789012:role/smartcd-platform-access"
API_GATEWAY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(API_GATEWAY_SRC, "..", ".."))


class FakeRequest:
    def __init__(self, tenant="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant})()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeDB:
    """In-memory aws_connections + a draft counter, enforcing the tenant filter the way RLS + the query do."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.drafts_using: dict[str, int] = {}
        self.deleted: list[str] = []

    async def execute(self, stmt, params=None):
        sql, p = " ".join(str(stmt).split()), params or {}
        if sql.startswith("INSERT INTO aws_connections"):
            if any(r["tenant_id"] == p["tenant"] and r["name"] == p["name"] for r in self.rows.values()):
                from sqlalchemy.exc import IntegrityError
                raise IntegrityError("dup", {}, Exception("unique"))
            self.rows[p["id"]] = dict(
                connection_id=p["id"], tenant_id=p["tenant"], name=p["name"], external_id=p["ext"], role_arn=None,
                aws_account_id=None, default_region=p["region"], status="PENDING", status_reason=None,
                verified_at=None, created_at=datetime.now(timezone.utc))
            return _Result([])
        if sql.startswith("SELECT * FROM aws_connections WHERE connection_id"):
            r = self.rows.get(p["id"])
            return _Result([r] if r and r["tenant_id"] == p["tenant"] else [])
        if sql.startswith("SELECT * FROM aws_connections WHERE tenant_id"):
            return _Result([r for r in self.rows.values() if r["tenant_id"] == p["tenant"]])
        if "SET status = 'VERIFIED'" in sql:
            r = self.rows[p["id"]]
            r.update(status="VERIFIED", status_reason=None, role_arn=p["arn"], aws_account_id=p["acct"], verified_at=datetime.now(timezone.utc))
            return _Result([])
        if "SET status = 'FAILED'" in sql:
            self.rows[p["id"]].update(status="FAILED", status_reason=p["reason"], role_arn=p["arn"])
            return _Result([])
        if sql.startswith("SELECT count(*) AS n FROM infra_build_state"):
            return _Result([{"n": self.drafts_using.get(p["id"], 0)}])
        if sql.startswith("DELETE FROM aws_connections"):
            self.deleted.append(p["id"])
            self.rows.pop(p["id"], None)
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=self)


class _Http:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _find(self, url):
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected URL {url}")

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._find(url)

    async def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        return self._find(url)


def _http(monkeypatch, verify=None):
    routes = {"platform-identity": _Resp(200, {"account_id": PLATFORM})}
    if verify is not None:
        routes["/aws-connections/verify"] = verify
    client = _Http(routes)
    monkeypatch.setattr(router_module.httpx, "AsyncClient", client)
    return client


def run(coro):
    return asyncio.run(coro)


# ─────────────── creating a connection ───────────────


def test_create_generates_a_high_entropy_platform_chosen_external_id_and_the_setup_template(monkeypatch):
    _http(monkeypatch)
    db = FakeDB()
    out = run(create_connection(CreateConnectionRequest(name="prod", default_region="eu-north-1"), FakeRequest(), db=db))

    assert out["status"] == "PENDING" and out["role_arn"] is None
    assert len(out["external_id"]) >= 43  # 32 random bytes, url-safe base64
    template = json.loads(out["cloudformation_template"])
    trust = template["Resources"]["PlatformAccessRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"]["AWS"] == f"arn:aws:iam::{PLATFORM}:root"
    assert trust["Condition"]["StringEquals"]["sts:ExternalId"] == out["external_id"]  # the customer never types it
    assert "smartcd-role.json" in out["cli_command"] and "eu-north-1" in out["cli_command"]


def test_every_connection_gets_a_different_external_id(monkeypatch):
    _http(monkeypatch)
    db = FakeDB()
    ids = {run(create_connection(CreateConnectionRequest(name=f"c{i}"), FakeRequest(), db=db))["external_id"] for i in range(20)}
    assert len(ids) == 20


def test_a_duplicate_name_in_the_same_tenant_is_a_409(monkeypatch):
    _http(monkeypatch)
    db = FakeDB()
    run(create_connection(CreateConnectionRequest(name="prod"), FakeRequest(), db=db))
    with pytest.raises(HTTPException) as e:
        run(create_connection(CreateConnectionRequest(name="prod"), FakeRequest(), db=db))
    assert e.value.status_code == 409


def test_the_same_name_is_fine_in_another_tenant(monkeypatch):
    _http(monkeypatch)
    db = FakeDB()
    run(create_connection(CreateConnectionRequest(name="prod"), FakeRequest("tenant-1"), db=db))
    run(create_connection(CreateConnectionRequest(name="prod"), FakeRequest("tenant-2"), db=db))


@pytest.mark.parametrize("bad", [
    dict(name=""), dict(name="x" * 81), dict(name="a;b"), dict(name="ok", default_region="us_east_1"),
    dict(name="ok", default_region="../../etc"),
])
def test_invalid_names_and_regions_are_rejected(bad):
    with pytest.raises(Exception):
        CreateConnectionRequest(**bad)


def test_the_platform_account_being_unknowable_is_a_502_not_a_template_with_a_wrong_principal(monkeypatch):
    monkeypatch.setattr(router_module.httpx, "AsyncClient", _Http({"platform-identity": _Resp(500, {"detail": "x"})}))
    with pytest.raises(HTTPException) as e:
        run(create_connection(CreateConnectionRequest(name="prod"), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 502


# ─────────────── verifying ───────────────


def _created(monkeypatch, db, tenant="tenant-1"):
    _http(monkeypatch)
    return run(create_connection(CreateConnectionRequest(name="prod"), FakeRequest(tenant), db=db))


def test_verify_sends_the_connections_own_external_id_and_marks_it_usable_only_on_success(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db)
    client = _http(monkeypatch, verify=_Resp(200, {"account_id": "123456789012", "arn": "arn:aws:sts::123456789012:assumed-role/x/y"}))

    out = run(verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn=ROLE), FakeRequest(), db=db))

    assert out["status"] == "VERIFIED" and out["aws_account_id"] == "123456789012" and out["role_arn"] == ROLE
    _, url, body = next(c for c in client.calls if "/aws-connections/verify" in c[1])
    assert body == {"role_arn": ROLE, "external_id": created["external_id"]}
    assert "external_id" not in out  # the list/verify view does not re-expose it


def test_a_refused_assume_role_marks_it_failed_with_the_reason_and_it_stays_unusable(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db)
    _http(monkeypatch, verify=_Resp(422, {"detail": "Could not assume the role (AccessDenied)"}))

    out = run(verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn=ROLE), FakeRequest(), db=db))

    assert out["status"] == "FAILED" and "AccessDenied" in out["status_reason"]
    with pytest.raises(HTTPException) as e:
        run(load_provisioning_connection(db, "tenant-1", created["connection_id"]))
    assert e.value.status_code == 422 and "not verified" in e.value.detail


def test_a_malformed_role_arn_never_reaches_pipeline_worker(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db)
    client = _http(monkeypatch, verify=_Resp(200, {}))
    with pytest.raises(HTTPException) as e:
        run(verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn="arn:aws:iam::123:user/bob-not-a-role"), FakeRequest(), db=db))
    assert e.value.status_code == 422
    assert not any("/aws-connections/verify" in c[1] for c in client.calls)


def test_verification_can_be_retried_after_a_failure(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db)
    _http(monkeypatch, verify=_Resp(422, {"detail": "AccessDenied"}))
    run(verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn=ROLE), FakeRequest(), db=db))
    _http(monkeypatch, verify=_Resp(200, {"account_id": "123456789012", "arn": "a"}))
    out = run(verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn=ROLE), FakeRequest(), db=db))
    assert out["status"] == "VERIFIED" and out["status_reason"] is None


# ─────────────── tenant isolation ───────────────


def test_another_tenant_cannot_see_verify_read_or_delete_a_connection(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db, tenant="tenant-1")
    other = FakeRequest("tenant-2")
    _http(monkeypatch, verify=_Resp(200, {"account_id": "123456789012", "arn": "a"}))

    assert run(list_connections(other, db=db)) == []
    for call in (
        lambda: get_connection(created["connection_id"], other, db=db),
        lambda: verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn=ROLE), other, db=db),
        lambda: delete_connection(created["connection_id"], other, db=db),
    ):
        with pytest.raises(HTTPException) as e:
            run(call())
        assert e.value.status_code == 404
    assert db.rows[created["connection_id"]]["status"] == "PENDING"  # untouched


def test_the_list_never_exposes_external_ids(monkeypatch):
    db = FakeDB()
    _created(monkeypatch, db)
    assert all("external_id" not in c and "cloudformation_template" not in c for c in run(list_connections(FakeRequest(), db=db)))


def test_a_malformed_id_is_a_404_not_a_500():
    with pytest.raises(HTTPException) as e:
        run(get_connection("not-a-uuid", FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 404


# ─────────────── using a connection for provisioning ───────────────


def _verified(monkeypatch, db):
    created = _created(monkeypatch, db)
    _http(monkeypatch, verify=_Resp(200, {"account_id": "123456789012", "arn": "a"}))
    run(verify_connection(created["connection_id"], VerifyConnectionRequest(role_arn=ROLE), FakeRequest(), db=db))
    return created


def test_a_verified_connection_yields_exactly_what_provisioning_needs(monkeypatch):
    db = FakeDB()
    created = _verified(monkeypatch, db)
    conn = run(load_provisioning_connection(db, "tenant-1", created["connection_id"]))
    assert conn == {"role_arn": ROLE, "external_id": created["external_id"], "default_region": "us-east-1"}


def test_no_connection_id_means_the_platforms_own_account_explicitly_none():
    assert run(load_provisioning_connection(FakeDB(), "tenant-1", None)) is None


def test_an_unknown_or_other_tenants_connection_is_an_error_never_a_silent_fallback_to_the_platform_account(monkeypatch):
    db = FakeDB()
    created = _verified(monkeypatch, db)
    with pytest.raises(HTTPException) as e:
        run(load_provisioning_connection(db, "tenant-2", created["connection_id"]))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        run(load_provisioning_connection(db, "tenant-1", "33333333-3333-3333-3333-333333333333"))


def test_an_unverified_connection_cannot_be_used(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db)
    with pytest.raises(HTTPException) as e:
        run(load_provisioning_connection(db, "tenant-1", created["connection_id"]))
    assert "not verified" in e.value.detail


# ─────────────── deleting ───────────────


def test_a_connection_still_used_by_drafts_cannot_be_deleted(monkeypatch):
    db = FakeDB()
    created = _verified(monkeypatch, db)
    db.drafts_using[created["connection_id"]] = 2
    with pytest.raises(HTTPException) as e:
        run(delete_connection(created["connection_id"], FakeRequest(), db=db))
    assert e.value.status_code == 409 and "2 infrastructure draft" in e.value.detail and "smartcd-platform-access" in e.value.detail
    assert db.deleted == []


def test_an_unused_connection_can_be_deleted(monkeypatch):
    db = FakeDB()
    created = _created(monkeypatch, db)
    assert run(delete_connection(created["connection_id"], FakeRequest(), db=db)) == {"deleted": created["connection_id"]}


# ─────────────── the role template ───────────────

EXT = "e" * 43


def _template():
    return tpl.build_role_template(PLATFORM, EXT)


def _policy_statements():
    return _template()["Resources"]["PlatformAccessRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def test_the_trust_policy_names_only_the_platform_account_and_requires_the_external_id():
    props = _template()["Resources"]["PlatformAccessRole"]["Properties"]
    (stmt,) = props["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Principal"] == {"AWS": f"arn:aws:iam::{PLATFORM}:root"} and stmt["Action"] == "sts:AssumeRole"
    assert stmt["Condition"] == {"StringEquals": {"sts:ExternalId": EXT}}
    assert props["MaxSessionDuration"] == 3600


def test_no_statement_grants_wildcard_actions_or_full_admin():
    for stmt in _policy_statements():
        actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        assert "*" not in actions and not any(a == "iam:*" for a in actions), stmt["Sid"]


def test_the_powerful_actions_are_scoped_to_platform_named_resources():
    by_sid = {s["Sid"]: s for s in _policy_statements()}
    assert "stack/smartcd-infra-*" in by_sid["ManageOnlyPlatformStacks"]["Resource"]["Fn::Sub"]
    assert by_sid["ManageOnlyPlatformRoles"]["Resource"]["Fn::Sub"].endswith("role/smartcd-*")
    assert by_sid["PassRolesOnlyToTheServicesThatNeedThem"]["Condition"]["StringEquals"]["iam:PassedToService"]
    assert by_sid["ManageOnlyPlatformBuckets"]["Resource"] == [{"Fn::Sub": "arn:${AWS::Partition}:s3:::smartcd-*"}]


def test_read_access_is_read_only():
    read = next(s for s in _policy_statements() if s["Sid"] == "ReadOnlyDiscoveryAndStatus")
    assert all(a.split(":")[1].startswith(("Describe", "List", "Get", "Validate")) for a in read["Action"])


def test_the_template_is_valid_json_and_outputs_the_role_arn_the_customer_pastes_back():
    parsed = json.loads(tpl.build_role_template_json(PLATFORM, EXT))
    assert parsed["Outputs"]["RoleArn"]["Value"] == {"Fn::GetAtt": ["PlatformAccessRole", "Arn"]}
    assert "Delete this stack to revoke all access" in parsed["Description"]


@pytest.mark.parametrize("account", ["123", "12345678901a", "", "1234567890123"])
def test_a_bad_platform_account_id_is_refused(account):
    with pytest.raises(ValueError):
        tpl.build_role_template(account, EXT)


def test_a_weak_external_id_is_refused():
    with pytest.raises(ValueError):
        tpl.build_role_template(PLATFORM, "short")


# ─────────────── deployment safety ───────────────


def test_the_gateway_still_imports_without_boto3():
    """
    Found before shipping: api-gateway's image has no boto3, and the first version of this feature imported it
    at module load - the gateway would have crashed on startup. Blocks boto3/botocore and imports the whole app.
    """
    code = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in ('boto3', 'botocore'):\n"
        "        raise ImportError('boto3 is not installed in api-gateway')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        f"sys.path[:0] = [{REPO_ROOT!r}, {API_GATEWAY_SRC!r}]\n"
        "import src.main\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=API_GATEWAY_SRC, timeout=120)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-800:]


def test_load_provisioning_connection_accepts_a_uuid_object():
    """Found live: a draft row hands back aws_connection_id as uuid.UUID, which used to 404 as 'not found'."""
    import asyncio, uuid
    from src.routers import aws_connections_router as r

    cid = uuid.uuid4()

    class Res:
        def mappings(self):
            return self

        def first(self):
            return {"status": "VERIFIED", "role_arn": "arn:aws:iam::123456789012:role/x", "external_id": "e",
                    "default_region": "us-east-1", "name": "n"}

    class DB:
        async def execute(self, *a, **k):
            return Res()

    out = asyncio.run(r.load_provisioning_connection(DB(), "t", cid))
    assert out["role_arn"].endswith(":role/x")
