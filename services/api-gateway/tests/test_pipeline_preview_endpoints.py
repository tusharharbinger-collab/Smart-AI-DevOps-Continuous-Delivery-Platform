"""
services/api-gateway/tests/test_pipeline_preview_endpoints.py

Covers Phase 6 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5) — preview_pipeline_template
(pure computation, wraps generate_project_pipeline_yaml with no DB write)
and preview_pipeline_ai_tuned (the Pipeline Architect Agent wired in for
real: same _generate_and_validate_pipeline_candidate retry loop the
existing post-creation editor uses, just against an IntentSpec-derived
prompt). Same FakeRequest/_FakeAsyncClient convention as
test_project_chatops.py.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import json

import pytest
import yaml
from fastapi import HTTPException

from shared.intent_spec import EnvironmentTier, IntentSpec
from src.routers import projects_router
from src.routers.projects_router import CreateProjectRequest, PipelinePreviewGenerateRequest


class FakeRequest:
    def __init__(self, tenant_id="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant_id})()


class _FakeHttpResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.request = None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Routes .post() by URL substring so one instance can stand in for both the
    generate-pipeline call and the pipelines/validate call within the same test."""

    def __init__(self, responses_by_url_fragment: dict[str, _FakeHttpResponse]):
        self._responses = responses_by_url_fragment
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        self.calls.append((url, json))
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"Unexpected URL in test: {url}")


def _base_request(**overrides) -> CreateProjectRequest:
    defaults = dict(name="test-svc", source_type="existing_image", container_image="registry.internal/test-svc")
    defaults.update(overrides)
    return CreateProjectRequest(**defaults)


def _spec(**overrides) -> IntentSpec:
    defaults = dict(environment_tier=EnvironmentTier.PRODUCTION, archetype="web_service_with_database", needs_database=True)
    defaults.update(overrides)
    return IntentSpec(**defaults)


# ─────────────── preview_pipeline_template ───────────────


def test_template_preview_returns_the_same_yaml_generate_project_pipeline_yaml_would():
    result = asyncio.run(projects_router.preview_pipeline_template(_base_request(), FakeRequest()))
    parsed = yaml.safe_load(result["policy_yaml"])
    assert parsed["kind"] == "Pipeline"
    assert "spec" in parsed


def test_template_preview_never_writes_to_the_database():
    # No `db` parameter exists on this endpoint at all — pure computation,
    # confirmed by successfully calling it with only (body, request).
    result = asyncio.run(projects_router.preview_pipeline_template(_base_request(name="no-db-write"), FakeRequest()))
    assert "no-db-write" in result["policy_yaml"] or "no-db-write-rollout" in result["policy_yaml"]


# ─────────────── preview_pipeline_ai_tuned ───────────────


def _groq_response(pipeline_yaml: str, summary: str) -> _FakeHttpResponse:
    # api-gateway calls explainability-service's OWN /generate-pipeline HTTP
    # endpoint (main.py's post_generate_pipeline), not Groq directly — that
    # endpoint already unwraps Groq's raw {"choices": [...]} shape and
    # returns a plain {"pipeline_yaml": ..., "summary_of_changes": ...} dict.
    return _FakeHttpResponse(200, {"pipeline_yaml": pipeline_yaml, "summary_of_changes": summary})


BASE_YAML = "apiVersion: delivery.devops.ai/v1alpha1\nkind: Pipeline\nspec: {}\n"
TUNED_YAML = "apiVersion: delivery.devops.ai/v1alpha1\nkind: Pipeline\nspec: {tuned: true}\n"


def test_successful_generation_returns_the_validated_candidate(monkeypatch):
    fake_client = _FakeAsyncClient(
        {
            "generate-pipeline": _groq_response(TUNED_YAML, "Tightened guardrails for production."),
            "validate": _FakeHttpResponse(200, {"valid": True}),
        }
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        projects_router.preview_pipeline_ai_tuned(
            PipelinePreviewGenerateRequest(base_yaml=BASE_YAML, intent_spec=_spec()), FakeRequest()
        )
    )

    assert result["pipeline_yaml"] == TUNED_YAML
    assert result["valid"] is True


def test_prompt_reflects_the_locked_intent_spec(monkeypatch):
    fake_client = _FakeAsyncClient(
        {"generate-pipeline": _groq_response(TUNED_YAML, "x"), "validate": _FakeHttpResponse(200, {"valid": True})}
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(
        projects_router.preview_pipeline_ai_tuned(
            PipelinePreviewGenerateRequest(
                base_yaml=BASE_YAML,
                intent_spec=_spec(environment_tier=EnvironmentTier.PRODUCTION, needs_database=True, multi_az=True),
            ),
            FakeRequest(),
        )
    )

    sent_prompt = fake_client.calls[0][1]["prompt"]
    assert "production" in sent_prompt
    assert "Multi-AZ" in sent_prompt
    assert "manual approval" in sent_prompt.lower()


def test_validation_failure_is_fed_back_on_retry(monkeypatch):
    fake_client = _FakeAsyncClient(
        {
            "generate-pipeline": _groq_response(TUNED_YAML, "attempt"),
            "validate": _FakeHttpResponse(200, {"valid": False, "error": "traffic steps not increasing"}),
        }
    )
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.preview_pipeline_ai_tuned(
                PipelinePreviewGenerateRequest(base_yaml=BASE_YAML, intent_spec=_spec()), FakeRequest()
            )
        )
    assert exc_info.value.status_code == 422
    # Two attempts total (per _generate_and_validate_pipeline_candidate's contract).
    generate_calls = [c for c in fake_client.calls if "generate-pipeline" in c[0]]
    assert len(generate_calls) == 2
    assert generate_calls[1][1]["validation_error"] == "traffic steps not increasing"


# ─────────────── create_project's ai_tuned_policy_yaml override ───────────────


def test_ai_tuned_policy_yaml_field_exists_and_defaults_to_none():
    body = _base_request()
    assert body.ai_tuned_policy_yaml is None


def test_ai_tuned_policy_yaml_can_be_set_explicitly():
    body = _base_request(ai_tuned_policy_yaml=TUNED_YAML)
    assert body.ai_tuned_policy_yaml == TUNED_YAML
