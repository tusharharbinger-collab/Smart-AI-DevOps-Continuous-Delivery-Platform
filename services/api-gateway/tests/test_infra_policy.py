"""
services/api-gateway/tests/test_infra_policy.py

Phase 7f - the independent OPA check on AI-generated infrastructure (src/infra_policy.py and its use in
projects_router.py). OPA itself is covered by policies/tests/infra_guardrails_test.rego; this covers OUR
integration: request shape, fail-closed behaviour, how findings reach the UI and the approval gate.
"""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException

from src import infra_policy
from src.routers import projects_router
from src.routers.projects_router import InfraDraftEditRequest, InfraDraftRequest

TEMPLATE = json.dumps({"Resources": {"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"StorageEncrypted": False}}}})
PROPOSAL = {
    "name": "x", "topology": {"nodes": [], "edges": []}, "cloudformation_template": TEMPLATE,
    "estimated_monthly_cost_usd": 40.0,
    "policy_checks": [{"check": "encryption_at_rest", "passed": True, "detail": "AI says fine"}],
}
DENY = {"rule": "rds_encryption_at_rest", "resource": "Db", "severity": "deny", "message": "Db: StorageEncrypted is not enabled"}
BLOCKED = {"allowed": False, "deny": [DENY], "warn": [], "checks": [
    {"check": "rds_encryption_at_rest", "description": "RDS storage is encrypted at rest", "status": "fail", "details": [DENY["message"]]},
    {"check": "within_budget", "description": "Within budget", "status": "pass", "details": []},
    {"check": "s3_not_public", "description": "S3 not public", "status": "warn", "details": ["B: no block"]},
]}
CLEAN = {"allowed": True, "deny": [], "warn": [], "checks": []}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=self)


class _OpaClient:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.resp


# ─────────────── evaluate_infra_policy ───────────────


def test_sends_the_parsed_template_intent_cost_and_existing_resources_to_the_infra_policy(monkeypatch):
    client = _OpaClient(_Resp(200, {"result": CLEAN}))
    monkeypatch.setattr(infra_policy.httpx, "AsyncClient", client)

    out = asyncio.run(infra_policy.evaluate_infra_policy(PROPOSAL, {"environment_tier": "dev"}, {"database": {"id": "d"}}))

    assert out == CLEAN
    url, body = client.calls[0]
    assert url.endswith("/v1/data/infra/guardrails/result")
    assert body["input"]["template"]["Resources"]["Db"]["Type"] == "AWS::RDS::DBInstance"  # parsed, not a string
    assert body["input"]["estimated_monthly_cost_usd"] == 40.0
    assert body["input"]["existing_resources"] == {"database": {"id": "d"}}
    assert body["input"]["intent_spec"] == {"environment_tier": "dev"}


def test_fails_closed_when_opa_is_unreachable(monkeypatch):
    monkeypatch.setattr(infra_policy.httpx, "AsyncClient", _OpaClient(exc=httpx.ConnectError("refused")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(infra_policy.evaluate_infra_policy(PROPOSAL, {}, None))
    assert e.value.status_code == 502 and "unchecked" in e.value.detail


def test_a_timeout_is_a_readable_502(monkeypatch):
    monkeypatch.setattr(infra_policy.httpx, "AsyncClient", _OpaClient(exc=httpx.ReadTimeout("")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(infra_policy.evaluate_infra_policy(PROPOSAL, {}, None))
    assert "ReadTimeout" in e.value.detail


def test_policy_not_loaded_is_not_mistaken_for_allowed(monkeypatch):
    # OPA answers 200 with an empty body when the policy isn't loaded - that must NOT read as "allowed".
    monkeypatch.setattr(infra_policy.httpx, "AsyncClient", _OpaClient(_Resp(200, {})))
    with pytest.raises(HTTPException) as e:
        asyncio.run(infra_policy.evaluate_infra_policy(PROPOSAL, {}, None))
    assert e.value.status_code == 502 and "loaded" in e.value.detail


def test_an_unparseable_template_is_blocked_without_calling_opa(monkeypatch):
    client = _OpaClient(_Resp(200, {"result": CLEAN}))
    monkeypatch.setattr(infra_policy.httpx, "AsyncClient", client)
    out = asyncio.run(infra_policy.evaluate_infra_policy({"cloudformation_template": "{not json"}, {}, None))
    assert out["allowed"] is False and client.calls == []


# ─────────────── apply_policy_to_proposal / blocking_messages ───────────────


def test_opa_checks_replace_the_models_own_and_the_models_are_kept_labelled():
    out = infra_policy.apply_policy_to_proposal(PROPOSAL, BLOCKED)

    assert out["ai_self_reported_policy_checks"] == PROPOSAL["policy_checks"]
    assert [c["status"] for c in out["policy_checks"]] == ["fail", "warn", "pass"]  # worst first
    assert out["policy_checks"][0]["passed"] is False and DENY["message"] in out["policy_checks"][0]["detail"]
    assert out["policy_checks"][1]["passed"] is True  # a warning does not fail the check
    assert out["policy_evaluation"]["engine"] == "opa" and out["policy_evaluation"]["allowed"] is False
    assert "policy_evaluation" not in PROPOSAL  # the input is not mutated


def test_blocking_messages_only_for_denied_proposals():
    assert infra_policy.blocking_messages(infra_policy.apply_policy_to_proposal(PROPOSAL, BLOCKED)) == [DENY["message"]]
    assert infra_policy.blocking_messages(infra_policy.apply_policy_to_proposal(PROPOSAL, CLEAN)) == []
    assert infra_policy.blocking_messages(PROPOSAL) == []  # a legacy draft with no evaluation is not retroactively blocked
    assert infra_policy.blocking_messages(None) == []


# ─────────────── router behaviour ───────────────

DRAFT_ID = "11111111-1111-1111-1111-111111111111"


class FakeRequest:
    def __init__(self):
        self.state = type("S", (), {"tenant_id": "tenant-1"})()


def _row(**over):
    base = dict(
        draft_id=DRAFT_ID, project_id=None, status="INFRA_PENDING_APPROVAL",
        intent_spec={"environment_tier": "dev", "archetype": "stateless_web_service", "aws_region": "us-east-1"},
        archetype="stateless_web_service", infra_proposal=infra_policy.apply_policy_to_proposal(PROPOSAL, BLOCKED),
        readiness_outcome="require_approval", readiness_reasons=[], error_message=None, cloud_provider="aws",
        change_set_id=None, stack_name=None, stack_arn=None, change_set_changes=[], provisioning_error=None,
        provisioning_outputs={}, source="ai_created", existing_resources={}, parent_draft_id=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return base


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeDB:
    def __init__(self, row=None):
        self.rows = {DRAFT_ID: row} if row else {}
        self.updates = []

    async def execute(self, stmt, params=None):
        sql, params = str(stmt), params or {}
        if "INSERT INTO infra_build_state" in sql:
            new_id = params.get("new_id") or params["draft_id"]
            self.rows[new_id] = _row(
                draft_id=new_id, status=params.get("status", "INFRA_DRAFTING"),
                intent_spec=json.loads(params["intent_spec"]), archetype=params["archetype"],
                infra_proposal=json.loads(params["proposal"]) if params.get("proposal") else None,
                readiness_outcome=params.get("outcome"), readiness_reasons=json.loads(params["reasons"]) if params.get("reasons") else [],
            )
        elif "SET status = :status, infra_proposal" in sql:
            self.updates.append(params)
            self.rows[params["draft_id"]].update(
                status=params["status"], infra_proposal=json.loads(params["proposal"]),
                readiness_outcome=params["outcome"], readiness_reasons=json.loads(params["reasons"]))
        elif "SET status = 'INFRA_APPROVED'" in sql:
            self.rows[params["draft_id"]]["status"] = "INFRA_APPROVED"
        elif "SELECT * FROM infra_build_state" in sql:
            return _Result(self.rows.get(params["draft_id"]))
        return _Result(None)


class _Http:
    def __init__(self, routes):
        self.routes = routes

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        for needle, payload in self.routes.items():
            if needle in url:
                r = _Resp(200, payload)
                r.text = ""
                return r
        raise AssertionError(url)


def _use_opa(monkeypatch, evaluation):
    async def evaluate(proposal, intent_spec, existing_resources):
        return evaluation

    monkeypatch.setattr(projects_router, "evaluate_infra_policy", evaluate)


def test_a_denied_proposal_never_auto_advances_even_when_readiness_says_it_could(monkeypatch):
    _use_opa(monkeypatch, BLOCKED)
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Http({"generate-infra": PROPOSAL}))
    # stateless + high-confidence + a small budget is the auto_advance case (see test_infra_drafts_endpoint.py).
    spec = InfraDraftRequest(environment_tier="dev", archetype="stateless_web_service", detected_confidence="high",
                             monthly_budget_usd=20, aws_region="us-east-1", needs_database=True)

    out = asyncio.run(projects_router.create_infra_draft(spec, FakeRequest(), db=FakeDB()))

    assert out["status"] == "INFRA_PENDING_APPROVAL"
    assert out["readiness_outcome"] == "require_approval"
    assert any("Blocked by infrastructure policy" in r for r in out["readiness_reasons"])
    assert out["infra_proposal"]["policy_evaluation"]["allowed"] is False


def test_a_clean_proposal_still_auto_advances(monkeypatch):
    _use_opa(monkeypatch, CLEAN)
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Http({"generate-infra": PROPOSAL}))
    spec = InfraDraftRequest(environment_tier="dev", archetype="stateless_web_service", detected_confidence="high",
                             monthly_budget_usd=20, aws_region="us-east-1", needs_database=True)

    out = asyncio.run(projects_router.create_infra_draft(spec, FakeRequest(), db=FakeDB()))

    assert out["status"] == "INFRA_APPROVED"


def test_approval_is_refused_for_a_policy_blocked_draft():
    db = FakeDB(_row())
    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.approve_infra_draft(DRAFT_ID, FakeRequest(), db=db))
    assert e.value.status_code == 409 and "StorageEncrypted" in e.value.detail
    assert db.rows[DRAFT_ID]["status"] == "INFRA_PENDING_APPROVAL"


def test_a_legacy_draft_without_an_evaluation_can_still_be_approved():
    db = FakeDB(_row(infra_proposal=PROPOSAL))
    out = asyncio.run(projects_router.approve_infra_draft(DRAFT_ID, FakeRequest(), db=db))
    assert out["status"] == "INFRA_APPROVED"


def test_editing_re_evaluates_policy_so_an_edit_can_fix_a_blocked_draft(monkeypatch):
    _use_opa(monkeypatch, CLEAN)
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Http({"generate-infra": PROPOSAL}))
    db = FakeDB(_row())

    out = asyncio.run(projects_router.edit_infra_draft(
        DRAFT_ID, InfraDraftEditRequest(instruction="enable encryption"), FakeRequest(), db=db))

    assert out["infra_proposal"]["policy_evaluation"]["allowed"] is True
    # ...and it is now approvable.
    approved = asyncio.run(projects_router.approve_infra_draft(out["draft_id"], FakeRequest(), db=db))
    assert approved["status"] == "INFRA_APPROVED"


def test_an_edit_that_stays_blocked_stays_unapprovable(monkeypatch):
    _use_opa(monkeypatch, BLOCKED)
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Http({"generate-infra": PROPOSAL}))
    db = FakeDB(_row())

    out = asyncio.run(projects_router.edit_infra_draft(
        DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=db))

    with pytest.raises(HTTPException) as e:
        asyncio.run(projects_router.approve_infra_draft(out["draft_id"], FakeRequest(), db=db))
    assert e.value.status_code == 409
