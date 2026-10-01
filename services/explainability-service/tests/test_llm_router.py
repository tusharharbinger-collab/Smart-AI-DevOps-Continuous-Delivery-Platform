"""
services/explainability-service/tests/test_llm_router.py

Covers shared/llm_router.py (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5) - the Gemini -> Groq ->
OpenRouter -> Mistral failover chain every LLM call site is meant to be migrated onto. Never touches a real
provider - the httpx call is monkeypatched, same convention as test_infra_generator.py /
test_pipeline_generator.py.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import asyncio

import httpx
import pytest

import shared.llm_router as llm_router
from shared.llm_router import (
    AllProvidersFailedError,
    call_llm,
    list_providers,
    set_provider_credential_override,
)
from shared.llm_router import test_provider_connection as check_provider_connection  # avoid pytest collecting this as a test


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Routes by URL to a per-provider canned response/exception, so a test can make ONE provider fail and
    confirm the router advances to the next rather than giving up."""

    def __init__(self, responses_by_url=None, exceptions_by_url=None):
        self._responses = responses_by_url or {}
        self._exceptions = exceptions_by_url or {}
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if url in self._exceptions:
            raise self._exceptions[url]
        return self._responses.get(url, _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}))


class _FakeRedis:
    def __init__(self, values=None):
        self._values = values or {}

    async def get(self, key):
        return self._values.get(key)

    async def set(self, key, value, ex=None):
        self._values[key] = value


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    for provider in llm_router.PROVIDERS:
        monkeypatch.delenv(provider.env_api_key, raising=False)
        monkeypatch.delenv(provider.env_model, raising=False)


def test_no_provider_configured_raises_with_no_attempts(monkeypatch):
    with pytest.raises(AllProvidersFailedError) as exc_info:
        asyncio.run(call_llm(messages=[{"role": "user", "content": "hi"}]))
    assert exc_info.value.attempts == []
    assert "No LLM provider is configured" in str(exc_info.value)


def test_unconfigured_providers_are_silently_skipped_only_groq_tried(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    fake_client = _FakeAsyncClient(
        responses_by_url={
            "https://api.groq.com/openai/v1/chat/completions": _FakeResponse(
                200, {"choices": [{"message": {"content": '{"ok": true}'}}]}
            )
        }
    )
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(call_llm(messages=[{"role": "user", "content": "hi"}]))

    assert result == {"content": '{"ok": true}', "provider": "groq", "model": "openai/gpt-oss-120b"}
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert fake_client.calls[0]["headers"]["Authorization"] == "Bearer test-groq-key"


def test_advances_to_next_provider_on_failure_in_priority_order(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    fake_client = _FakeAsyncClient(
        exceptions_by_url={
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions": httpx.ConnectError("down"),
        },
        responses_by_url={
            "https://api.groq.com/openai/v1/chat/completions": _FakeResponse(
                200, {"choices": [{"message": {"content": "groq answered"}}]}
            ),
        },
    )
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(call_llm(messages=[{"role": "user", "content": "hi"}]))

    assert result["provider"] == "groq"
    assert result["content"] == "groq answered"
    # Gemini was actually tried first (priority order), Groq only after it failed.
    assert [c["url"] for c in fake_client.calls] == [
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "https://api.groq.com/openai/v1/chat/completions",
    ]


def test_all_providers_failing_raises_with_every_attempt_recorded(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key")
    fake_client = _FakeAsyncClient(
        exceptions_by_url={
            "https://api.groq.com/openai/v1/chat/completions": httpx.TimeoutException("slow"),
            "https://api.mistral.ai/v1/chat/completions": httpx.ConnectError("down"),
        }
    )
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(AllProvidersFailedError) as exc_info:
        asyncio.run(call_llm(messages=[{"role": "user", "content": "hi"}]))

    providers_attempted = {a["provider"] for a in exc_info.value.attempts}
    assert providers_attempted == {"groq", "mistral"}


def test_redis_override_takes_precedence_over_env_var(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    redis = _FakeRedis({"llm_provider:api_key_override:groq": "override-key"})
    fake_client = _FakeAsyncClient(
        responses_by_url={
            "https://api.groq.com/openai/v1/chat/completions": _FakeResponse(
                200, {"choices": [{"message": {"content": "ok"}}]}
            )
        }
    )
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(call_llm(messages=[{"role": "user", "content": "hi"}], redis_client=redis))

    assert fake_client.calls[0]["headers"]["Authorization"] == "Bearer override-key"


def test_list_providers_masks_credentials_and_reports_configured_status(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-abcdefghijklmnop")

    providers = asyncio.run(list_providers())

    by_name = {p["name"]: p for p in providers}
    assert by_name["groq"]["configured"] is True
    assert by_name["groq"]["masked_credential"] == "sk-a****mnop"
    assert by_name["gemini"]["configured"] is False
    assert by_name["gemini"]["masked_credential"] == ""
    # Priority order preserved (Gemini first, Mistral last) regardless of which are configured.
    assert [p["name"] for p in providers] == ["gemini", "groq", "openrouter", "mistral"]


def test_test_provider_connection_reports_ok_on_success(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    fake_client = _FakeAsyncClient(
        responses_by_url={"https://api.groq.com/openai/v1/chat/completions": _FakeResponse(200, {"choices": []})}
    )
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(check_provider_connection("groq"))

    assert result["ok"] is True
    assert result["latency_ms"] is not None


def test_test_provider_connection_reports_not_configured(monkeypatch):
    result = asyncio.run(check_provider_connection("mistral"))
    assert result == {"ok": False, "latency_ms": None, "error": "No credential configured for this provider."}


def test_test_provider_connection_unknown_name_raises():
    with pytest.raises(ValueError):
        asyncio.run(check_provider_connection("does-not-exist"))


def test_set_provider_credential_override_writes_to_redis():
    redis = _FakeRedis()
    asyncio.run(set_provider_credential_override("mistral", "  new-secret-key  ", redis))
    assert redis._values["llm_provider:api_key_override:mistral"] == "new-secret-key"


def test_set_provider_credential_override_rejects_unknown_provider():
    with pytest.raises(ValueError):
        asyncio.run(set_provider_credential_override("does-not-exist", "key", _FakeRedis()))


def test_set_provider_credential_override_rejects_empty_key():
    with pytest.raises(ValueError):
        asyncio.run(set_provider_credential_override("groq", "   ", _FakeRedis()))
