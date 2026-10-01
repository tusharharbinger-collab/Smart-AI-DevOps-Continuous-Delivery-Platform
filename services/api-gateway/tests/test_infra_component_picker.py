"""
services/api-gateway/tests/test_infra_component_picker.py

Covers get_infra_component_catalog and check_infra_component (projects_router.py) -
AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2 (Phase C). Same test convention as
test_project_chatops.py: call the router function directly, mock the httpx call to
explainability-service and (where needed) a fake DB.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from src.routers import projects_router
from src.routers.projects_router import CheckComponentRequest


class FakeRequest:
    def __init__(self, tenant_id="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant_id})()


class _FakeHttpResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.last_json = None
        self.last_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.last_url = url
        if self._exc:
            raise self._exc
        return self._response

    async def post(self, url, json=None, **kwargs):
        self.last_url = url
        self.last_json = json
        if self._exc:
            raise self._exc
        return self._response


class FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeDB:
    def __init__(self, row=None):
        self._row = row

    async def execute(self, stmt, params=None):
        return FakeResult(self._row)


def test_get_infra_component_catalog_proxies_the_real_response(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, {"catalog": [{"resource_type": "ec2_instance"}]}))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(projects_router.get_infra_component_catalog())

    assert result == {"catalog": [{"resource_type": "ec2_instance"}]}
    assert fake_client.last_url.endswith("/component-catalog")


def test_get_infra_component_catalog_502s_when_unreachable(monkeypatch):
    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("down"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(projects_router.get_infra_component_catalog())
    assert exc_info.value.status_code == 502


def test_check_infra_component_404s_when_draft_not_found(monkeypatch):
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.check_infra_component(
                "11111111-1111-1111-1111-111111111111",
                CheckComponentRequest(resource_type="ec2_instance", params={}),
                FakeRequest(),
                db=FakeDB(row=None),
            )
        )
    assert exc_info.value.status_code == 404


def test_check_infra_component_forwards_archetype_and_hardcoded_deploy_target(monkeypatch):
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")
    fake_client = _FakeAsyncClient(
        _FakeHttpResponse(200, {"compatible": True, "explanation": "fine", "caveats": [], "source": "rule_table"})
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    row = {"archetype": "stateless_web_service", "intent_spec": {"aws_region": "us-east-1"}}
    result = asyncio.run(
        projects_router.check_infra_component(
            "11111111-1111-1111-1111-111111111111",
            CheckComponentRequest(resource_type="ec2_instance", params={"instance_type": "t3.micro"}),
            FakeRequest(),
            db=FakeDB(row=row),
        )
    )

    assert result == {"compatible": True, "explanation": "fine", "caveats": [], "source": "rule_table"}
    assert fake_client.last_json == {
        "resource_type": "ec2_instance",
        "params": {"instance_type": "t3.micro"},
        "archetype": "stateless_web_service",
        "deploy_target": "aws_ecs",
        "intent_spec": {"aws_region": "us-east-1"},
    }


def test_check_infra_component_502s_when_explainability_service_fails(monkeypatch):
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")
    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("down"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    row = {"archetype": "stateless_web_service", "intent_spec": {}}
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.check_infra_component(
                "11111111-1111-1111-1111-111111111111",
                CheckComponentRequest(resource_type="ec2_instance", params={}),
                FakeRequest(),
                db=FakeDB(row=row),
            )
        )
    assert exc_info.value.status_code == 502
