"""
services/explainability-service/tests/test_pipeline_generator.py

Never touches a real Groq account — the httpx call is monkeypatched. No
async test plugin is configured in this service yet (see pyproject.toml),
so these wrap the async function with asyncio.run() inside plain sync
test functions rather than introducing a new dependency for this alone.
"""
import asyncio
import json

import httpx
import pytest

import src.pipeline_generator as pipeline_generator
from src.pipeline_generator import PipelineGenerationError, generate_pipeline_yaml

SAMPLE_YAML = "apiVersion: delivery.devops.ai/v1alpha1\nkind: Pipeline\n"


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


def _groq_payload(pipeline_yaml: str, summary: str) -> dict:
    return {
        "choices": [
            {"message": {"content": json.dumps({"pipeline_yaml": pipeline_yaml, "summary_of_changes": summary})}}
        ]
    }


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.last_json_body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.last_json_body = json
        if self._exc:
            raise self._exc
        return self._response


def test_generates_valid_yaml_from_a_successful_groq_call(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(SAMPLE_YAML, "Added a 15% canary step.")))
    monkeypatch.setattr(pipeline_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        generate_pipeline_yaml(prompt="add a step", current_yaml=SAMPLE_YAML, context={"service_name": "orders"})
    )

    assert result["pipeline_yaml"] == SAMPLE_YAML
    assert result["summary_of_changes"] == "Added a 15% canary step."


def test_validation_error_is_included_in_the_retry_prompt(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(SAMPLE_YAML, "Fixed the step ordering.")))
    monkeypatch.setattr(pipeline_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(
        generate_pipeline_yaml(
            prompt="add a step",
            current_yaml=SAMPLE_YAML,
            context={},
            validation_error="Traffic steps must be strictly increasing",
        )
    )

    sent_prompt = fake_client.last_json_body["messages"][1]["content"]
    assert "Traffic steps must be strictly increasing" in sent_prompt


def test_raises_clear_error_when_groq_api_key_not_configured(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(PipelineGenerationError, match="GROQ_API_KEY"):
        asyncio.run(generate_pipeline_yaml(prompt="add a step", current_yaml=SAMPLE_YAML, context={}))


def test_raises_clear_error_on_groq_timeout(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(exc=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(pipeline_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(PipelineGenerationError, match="timed out"):
        asyncio.run(generate_pipeline_yaml(prompt="add a step", current_yaml=SAMPLE_YAML, context={}))


def test_raises_clear_error_on_malformed_groq_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    bad_response = _FakeResponse({"choices": [{"message": {"content": "not valid json"}}]})
    fake_client = _FakeAsyncClient(bad_response)
    monkeypatch.setattr(pipeline_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(PipelineGenerationError):
        asyncio.run(generate_pipeline_yaml(prompt="add a step", current_yaml=SAMPLE_YAML, context={}))
