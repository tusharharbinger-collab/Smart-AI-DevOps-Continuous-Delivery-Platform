"""
services/explainability-service/tests/test_digest_summarizer.py

Mirrors test_stage_failure_analyzer.py's mocking approach. Same
non-negotiable guarantee: the digest itself has already been computed by
digest_generator.py by the time this runs, so a Groq outage must degrade
to the deterministic fallback rather than ever raising or blocking the
digest response.
"""
import asyncio
import json

import httpx

import src.digest_summarizer as digest_summarizer
from src.digest_summarizer import generate_digest_summary


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _groq_payload(summary: str, trend: str, points: list[str]) -> dict:
    return {
        "choices": [
            {"message": {"content": json.dumps({"summary": summary, "notable_trend": trend, "talking_points": points})}}
        ]
    }


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        if self._exc:
            raise self._exc
        return self._response


_DIGEST = {
    "tenant_id": "tenant-1",
    "period_days": 7,
    "total_deployments": 20,
    "rollback_count": 4,
    "rollback_rate_percent": 20.0,
    "mean_time_to_verify_seconds": 145.0,
    "pipeline_success_rate_percent": 80.0,
    "avg_cost_delta_percent": 3.5,
}


def test_returns_fallback_when_groq_api_key_not_configured(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = asyncio.run(generate_digest_summary(_DIGEST))

    assert result["notable_trend"] == "degrading"
    assert "20" in result["summary"]


def test_returns_real_grounded_summary_from_a_successful_groq_call(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(
        _FakeResponse(_groq_payload("Rollback rate rose to 20% this period.", "degrading", ["20% rollback rate", "80% success rate"]))
    )
    monkeypatch.setattr(digest_summarizer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(generate_digest_summary(_DIGEST))

    assert result["summary"] == "Rollback rate rose to 20% this period."
    assert result["notable_trend"] == "degrading"
    assert result["talking_points"] == ["20% rollback rate", "80% success rate"]


def test_falls_back_on_groq_timeout_never_raises(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(exc=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(digest_summarizer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(generate_digest_summary(_DIGEST))

    assert result["notable_trend"] in ("improving", "stable", "degrading")


def test_falls_back_on_malformed_groq_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse({"choices": [{"message": {"content": "not valid json"}}]}))
    monkeypatch.setattr(digest_summarizer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(generate_digest_summary(_DIGEST))

    assert result["notable_trend"] == "degrading"


def test_fallback_zero_deployments_is_stable_not_degrading():
    result = asyncio.run(
        generate_digest_summary(
            {"tenant_id": "t", "period_days": 7, "total_deployments": 0, "rollback_count": 0, "rollback_rate_percent": 0}
        )
    )
    assert result["notable_trend"] == "stable"
    assert "No deployments" in result["summary"]


def test_fallback_zero_rollbacks_with_deployments_is_improving():
    result = asyncio.run(
        generate_digest_summary(
            {
                "tenant_id": "t", "period_days": 7, "total_deployments": 10, "rollback_count": 0,
                "rollback_rate_percent": 0, "pipeline_success_rate_percent": 100,
            }
        )
    )
    assert result["notable_trend"] == "improving"


def test_prompt_includes_the_real_digest_numbers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured = {}

    class _CapturingClient(_FakeAsyncClient):
        async def post(self, url, headers=None, json=None):
            captured["prompt"] = json["messages"][1]["content"]
            return _FakeResponse(_groq_payload("s", "stable", []))

    monkeypatch.setattr(digest_summarizer.httpx, "AsyncClient", lambda **kw: _CapturingClient())

    asyncio.run(generate_digest_summary(_DIGEST))

    assert "20" in captured["prompt"]  # rollback_count / rollback_rate
    assert "tenant-1" in captured["prompt"]
    assert "80.0" in captured["prompt"]  # pipeline_success_rate_percent
