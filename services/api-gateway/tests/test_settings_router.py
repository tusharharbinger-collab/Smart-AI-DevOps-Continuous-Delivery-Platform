"""
services/api-gateway/tests/test_settings_router.py

Covers settings_router.py - the thin, role-gated proxy in front of explainability-service's LLM-provider
settings (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5). Same test convention as
test_project_chatops.py: call the router function directly, mock only the httpx call to
explainability-service.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from src.routers import settings_router
from src.routers.settings_router import SetProviderCredentialRequest


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        if self._exc:
            raise self._exc
        return self._response

    async def post(self, url, json=None):
        if self._exc:
            raise self._exc
        return self._response


def test_list_llm_providers_proxies_the_real_response(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, {"providers": [{"name": "groq", "configured": True}]}))
    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(settings_router.list_llm_providers())

    assert result == {"providers": [{"name": "groq", "configured": True}]}


def test_list_llm_providers_502s_when_explainability_service_unreachable(monkeypatch):
    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(settings_router.list_llm_providers())
    assert exc_info.value.status_code == 502


def test_test_llm_provider_proxies_the_real_result(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, {"ok": True, "latency_ms": 850, "error": None}))
    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(settings_router.test_llm_provider("groq"))

    assert result == {"ok": True, "latency_ms": 850, "error": None}


def test_test_llm_provider_404s_for_unknown_provider(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(404, text="Unknown provider: bogus"))
    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(settings_router.test_llm_provider("bogus"))
    assert exc_info.value.status_code == 404


def test_set_llm_provider_credentials_proxies_when_role_allows(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, {"ok": True}))
    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        settings_router.set_llm_provider_credentials(
            "mistral", SetProviderCredentialRequest(api_key="new-secret"), role="lead-sre"
        )
    )

    assert result == {"ok": True}


def test_set_llm_provider_credentials_422s_on_empty_key(monkeypatch):
    fake_client = _FakeAsyncClient(_FakeHttpResponse(422, {"detail": "API key must not be empty."}))
    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            settings_router.set_llm_provider_credentials(
                "mistral", SetProviderCredentialRequest(api_key=""), role="lead-sre"
            )
        )
    assert exc_info.value.status_code == 422
