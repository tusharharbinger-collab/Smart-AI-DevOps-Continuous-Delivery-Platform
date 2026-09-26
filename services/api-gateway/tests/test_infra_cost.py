"""
services/api-gateway/tests/test_infra_cost.py

Phase 7e integration - src/infra_cost.py and its use in projects_router.py. The estimator arithmetic and
price selection are covered in pipeline-worker's tests; this covers request shape, how the result
replaces the model's number, and the fail-soft-but-never-silent behaviour.
"""
import asyncio
import json

import httpx
import pytest

from src import infra_cost
from src.routers import projects_router
from src.routers.projects_router import InfraDraftEditRequest, InfraDraftRequest

TEMPLATE = json.dumps({"Resources": {"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"DBInstanceClass": "db.t3.micro"}}}})
PROPOSAL = {
    "name": "x", "topology": {"nodes": [], "edges": []}, "cloudformation_template": TEMPLATE,
    "estimated_monthly_cost_usd": 999.0, "cost_breakdown": [{"resource": "ai-guess", "monthly_usd": 999.0}],
    "policy_checks": [],
}
ESTIMATE = {
    "source": "aws-price-list", "currency": "USD", "total_monthly_usd": 15.44,
    "items": [{"resource": "Db", "type": "AWS::RDS::DBInstance", "monthly_usd": 13.14, "basis": "db.t3.micro @ $0.018/hr"},
              {"resource": "Db storage", "type": "AWS::RDS::DBInstance/Storage", "monthly_usd": 2.30, "basis": "20 GB gp2"}],
    "no_fixed_cost": ["AWS::EC2::SecurityGroup"], "unpriced": [{"resource": "X", "type": "AWS::Foo::Bar", "reason": "not priced"}],
    "assumptions": ["730 hours/month"], "errors": 0,
}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=self)


class _Client:
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


def _run(monkeypatch, client, proposal=PROPOSAL, region="us-east-1"):
    monkeypatch.setattr(infra_cost.httpx, "AsyncClient", client)
    return asyncio.run(infra_cost.apply_independent_cost(proposal, region))


def test_the_computed_cost_replaces_the_models_and_the_models_is_kept_labelled(monkeypatch):
    client = _Client(_Resp(200, ESTIMATE))
    out = _run(monkeypatch, client)

    assert out["estimated_monthly_cost_usd"] == 15.44
    assert out["ai_estimated_monthly_cost_usd"] == 999.0
    assert out["ai_cost_breakdown"] == [{"resource": "ai-guess", "monthly_usd": 999.0}]
    assert out["cost_breakdown"][0] == {"resource": "Db - db.t3.micro @ $0.018/hr", "monthly_usd": 13.14}
    ce = out["cost_estimate"]
    assert ce["source"] == "aws-price-list" and ce["unpriced"][0]["resource"] == "X" and ce["assumptions"] == ["730 hours/month"]
    assert PROPOSAL["estimated_monthly_cost_usd"] == 999.0  # input not mutated


def test_sends_the_parsed_template_and_region_to_pipeline_worker(monkeypatch):
    client = _Client(_Resp(200, ESTIMATE))
    _run(monkeypatch, client, region="eu-west-1")
    url, body = client.calls[0]
    assert url.endswith("/infra-provisioning/estimate-cost")
    assert body["region"] == "eu-west-1" and body["template"]["Resources"]["Db"]["Type"] == "AWS::RDS::DBInstance"


def test_pricing_unreachable_keeps_the_ai_number_but_labels_it_unverified(monkeypatch):
    out = _run(monkeypatch, _Client(exc=httpx.ConnectError("refused")))
    assert out["estimated_monthly_cost_usd"] == 999.0
    assert out["cost_estimate"]["source"] == "ai_estimate_unverified"
    assert "unavailable" in out["cost_estimate"]["reason"]
    assert "ai_estimated_monthly_cost_usd" not in out


def test_a_timeout_reason_is_never_empty(monkeypatch):
    out = _run(monkeypatch, _Client(exc=httpx.ReadTimeout("")))
    assert "ReadTimeout" in out["cost_estimate"]["reason"]


def test_all_lookups_failing_is_unknown_not_free(monkeypatch):
    failed = {**ESTIMATE, "items": [], "total_monthly_usd": 0.0, "errors": 1,
              "unpriced": [{"resource": "Db", "type": "AWS::RDS::DBInstance", "reason": "price lookup failed: AmazonRDS: AccessDenied"}]}
    out = _run(monkeypatch, _Client(_Resp(200, failed)))
    assert out["estimated_monthly_cost_usd"] == 999.0  # NOT replaced with $0
    assert out["cost_estimate"]["source"] == "ai_estimate_unverified" and "AccessDenied" in out["cost_estimate"]["reason"]


def test_a_template_of_only_free_resources_is_legitimately_zero(monkeypatch):
    free = {**ESTIMATE, "items": [], "total_monthly_usd": 0.0, "errors": 0, "unpriced": []}
    out = _run(monkeypatch, _Client(_Resp(200, free)))
    assert out["estimated_monthly_cost_usd"] == 0.0 and out["cost_estimate"]["source"] == "aws-price-list"


def test_partial_lookup_failures_still_use_the_priced_items_and_report_the_errors(monkeypatch):
    partial = {**ESTIMATE, "errors": 1,
               "unpriced": [{"resource": "Cache", "type": "AWS::ElastiCache::CacheCluster", "reason": "price lookup failed: x"}]}
    out = _run(monkeypatch, _Client(_Resp(200, partial)))
    assert out["estimated_monthly_cost_usd"] == 15.44 and out["cost_estimate"]["errors"] == 1


def test_an_unparseable_template_is_unverified_without_calling_pricing(monkeypatch):
    client = _Client(_Resp(200, ESTIMATE))
    out = _run(monkeypatch, client, proposal={**PROPOSAL, "cloudformation_template": "{oops"})
    assert out["cost_estimate"]["source"] == "ai_estimate_unverified" and client.calls == []


# ─────────────── router: order and reach ───────────────

DRAFT_ID = "11111111-1111-1111-1111-111111111111"


def test_create_prices_before_policy_so_the_budget_rule_uses_the_real_number(monkeypatch):
    order = []

    async def cost(proposal, region):
        order.append("cost")
        return {**proposal, "estimated_monthly_cost_usd": 15.44}

    async def policy(proposal, intent_spec, existing):
        order.append(("policy", proposal["estimated_monthly_cost_usd"]))
        return {"allowed": True, "deny": [], "warn": [], "checks": []}

    monkeypatch.setattr(projects_router, "apply_independent_cost", cost)
    monkeypatch.setattr(projects_router, "evaluate_infra_policy", policy)

    class Http:
        def __call__(self, **kw):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            return _Resp(200, dict(PROPOSAL))

    monkeypatch.setattr(projects_router.httpx, "AsyncClient", Http())

    from tests.test_infra_import_and_edit_endpoints import FakeDB, FakeRequest  # reuse the in-memory DB double

    spec = InfraDraftRequest(environment_tier="dev", archetype="web_service_with_database", aws_region="us-east-1", needs_database=True)
    out = asyncio.run(projects_router.create_infra_draft(spec, FakeRequest(), db=FakeDB()))

    assert order == ["cost", ("policy", 15.44)]  # OPA saw the computed number, not the model's 999
    assert out["infra_proposal"]["estimated_monthly_cost_usd"] == 15.44
