"""
"Decide what to build before asking the AI": a self-contained web app needs NOTHING extra (the platform already builds
the ALB, cluster and baseline/canary services); only databases, caches, storage and worker services need the agent.
"""
import asyncio

import pytest
from fastapi import HTTPException

from shared.infra_needs import analyze_infra_needs, build_standard_only_proposal
from src.routers import projects_router
from src.routers.projects_router import InfraDraftRequest
from tests.test_infra_import_and_edit_endpoints import DRAFT_ID, FakeDB, FakeRequest, PROPOSAL, _Client, _Resp, _row


def run(coro):
    return asyncio.run(coro)


# ───────── the decision itself ─────────


def test_a_plain_web_app_needs_nothing_extra():
    n = analyze_infra_needs({"needs_database": False, "needs_cache": False, "needs_object_storage": False}, "stateless_web_service")
    assert n["needs_ai"] is False and n["additions"] == []
    assert "Nothing extra needs to be built" in n["summary"]


def test_a_static_site_needs_nothing_extra():
    assert analyze_infra_needs({}, "static_site")["needs_ai"] is False


@pytest.mark.parametrize("flag,kind", [("needs_database", "database"), ("needs_cache", "cache"), ("needs_object_storage", "object_storage")])
def test_each_data_flag_adds_exactly_its_own_extra(flag, kind):
    n = analyze_infra_needs({flag: True}, "stateless_web_service")
    assert n["needs_ai"] is True and [a["kind"] for a in n["additions"]] == [kind]


def test_database_engine_is_named_in_the_reason():
    assert "MySQL" in analyze_infra_needs({"needs_database": True, "database_type": "mysql"}, "x")["additions"][0]["reason"]


def test_the_human_can_opt_out_even_when_the_archetype_suggests_a_database():
    """Flags are the human-locked truth; the archetype alone never adds infrastructure."""
    assert analyze_infra_needs({"needs_database": False}, "web_service_with_database")["needs_ai"] is False


@pytest.mark.parametrize("archetype,kind", [("background_worker", "worker_service"), ("multi_service", "extra_services")])
def test_worker_and_multi_service_need_the_agent(archetype, kind):
    assert [a["kind"] for a in analyze_infra_needs({}, archetype)["additions"]] == [kind]


def test_attaching_existing_resources_needs_the_agent_even_with_no_additions():
    n = analyze_infra_needs({}, "stateless_web_service", {"database": {"id": "db-1"}})
    assert n["needs_ai"] is True and "attach" in n["summary"].lower()


def test_the_standard_only_proposal_has_no_template_no_cost_and_lists_what_the_platform_provides():
    n = analyze_infra_needs({}, "stateless_web_service")
    p = build_standard_only_proposal({"environment_tier": "dev"}, "stateless_web_service", n)
    assert p["no_additional_infrastructure"] is True and p["cloudformation_template"] == ""
    assert p["estimated_monthly_cost_usd"] == 0.0 and p["cost_estimate"]["source"] == "none"
    assert any("load balancer" in x.lower() for x in p["platform_provides"])
    assert {n["id"] for n in p["topology"]["nodes"]} == {"alb", "baseline", "canary"}


# ───────── through the API ─────────


def spec(**over):
    base = dict(environment_tier="dev", archetype="stateless_web_service", aws_region="us-east-1")
    base.update(over)
    return InfraDraftRequest(**base)


def test_creating_a_draft_for_a_plain_app_never_calls_the_ai(monkeypatch):
    client = _Client({})  # any call at all would raise "unexpected URL"
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    out = run(projects_router.create_infra_draft(spec(), FakeRequest(), db=FakeDB()))
    assert client.calls == []
    assert out["infra_proposal"]["no_additional_infrastructure"] is True
    assert out["infra_proposal"]["estimated_monthly_cost_usd"] == 0.0


def test_creating_a_draft_with_a_database_sends_only_the_extras_and_the_real_network(monkeypatch):
    client = _Client({"generate-infra": _Resp(200, PROPOSAL)})
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    out = run(projects_router.create_infra_draft(spec(needs_database=True), FakeRequest(), db=FakeDB()))
    sent = next(c for c in client.calls if "generate-infra" in c[1])[2]
    assert [a["kind"] for a in sent["additions"]] == ["database"]
    assert sent["platform_context"]["vpc_id"] == "vpc-test"
    assert out["infra_proposal"]["no_additional_infrastructure"] is False
    assert out["infra_proposal"]["additions"][0]["kind"] == "database"


def test_a_failed_network_lookup_is_a_clear_error_not_a_made_up_vpc(monkeypatch):
    async def boom(region, connection):
        raise HTTPException(status_code=502, detail="Could not read the platform network: nope")

    monkeypatch.setattr(projects_router, "_platform_network_context", boom)
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({"generate-infra": _Resp(200, PROPOSAL)}))
    with pytest.raises(HTTPException) as e:
        run(projects_router.create_infra_draft(spec(needs_database=True), FakeRequest(), db=FakeDB()))
    assert e.value.status_code == 502 and "platform network" in e.value.detail


def _no_extras_db():
    n = analyze_infra_needs({}, "stateless_web_service")
    db = FakeDB()
    db.rows[DRAFT_ID] = _row(status="INFRA_APPROVED", infra_proposal=build_standard_only_proposal({}, "stateless_web_service", n))
    return db


def test_editing_a_draft_that_needs_nothing_says_how_to_add_something(monkeypatch):
    from src.routers.projects_router import InfraDraftEditRequest
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({}))
    with pytest.raises(HTTPException) as e:
        run(projects_router.edit_infra_draft(DRAFT_ID, InfraDraftEditRequest(instruction="add a cache"), FakeRequest(), db=_no_extras_db()))
    assert e.value.status_code == 409 and "Requirements" in e.value.detail


def test_there_is_no_change_set_to_preview_when_nothing_is_provisioned(monkeypatch):
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", _Client({}))
    with pytest.raises(HTTPException) as e:
        run(projects_router.create_infra_change_set(DRAFT_ID, FakeRequest(), db=_no_extras_db()))
    assert e.value.status_code == 409 and "Nothing to provision" in e.value.detail
