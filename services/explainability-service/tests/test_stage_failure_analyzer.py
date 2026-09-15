"""
services/explainability-service/tests/test_stage_failure_analyzer.py

Mirrors test_pipeline_generator.py's mocking approach. Unlike pipeline
generation, this MUST always return something usable — the pipeline has
already been marked FAILED by the time this runs, so a Groq outage must
degrade to the deterministic fallback rather than ever raising.
"""
import asyncio
import json

import httpx

import src.stage_failure_analyzer as stage_failure_analyzer
from src.stage_failure_analyzer import generate_stage_failure_rca


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _groq_payload(cause: str, evidence: list[str], fix: str) -> dict:
    return {
        "choices": [
            {"message": {"content": json.dumps({"likely_cause": cause, "evidence": evidence, "suggested_fix": fix})}}
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


def test_returns_fallback_when_groq_api_key_not_configured(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = asyncio.run(
        generate_stage_failure_rca(
            run_id="run-1", failed_stage="build", error_message="No such file or directory", recent_logs=[]
        )
    )

    assert "build" in result["likely_cause"]
    assert "No such file or directory" in result["likely_cause"]
    assert result["evidence"] == ["No such file or directory"]


def test_returns_real_grounded_rca_from_a_successful_groq_call(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(
        _FakeResponse(_groq_payload("pytest module not found", ["ModuleNotFoundError: No module named pytest"], "Add pytest to requirements.txt"))
    )
    monkeypatch.setattr(stage_failure_analyzer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        generate_stage_failure_rca(
            run_id="run-1",
            failed_stage="test",
            error_message="No module named pytest",
            recent_logs=["ModuleNotFoundError: No module named pytest"],
        )
    )

    assert result["likely_cause"] == "pytest module not found"
    assert result["suggested_fix"] == "Add pytest to requirements.txt"


def test_falls_back_on_groq_timeout_never_raises(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(exc=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(stage_failure_analyzer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        generate_stage_failure_rca(run_id="run-1", failed_stage="build", error_message="build error", recent_logs=[])
    )

    assert "build error" in result["likely_cause"]


def test_only_sends_the_last_50_log_lines(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured = {}

    class _CapturingClient(_FakeAsyncClient):
        async def post(self, url, headers=None, json=None):
            captured["prompt"] = json["messages"][1]["content"]
            return _FakeResponse(_groq_payload("cause", [], "fix"))

    monkeypatch.setattr(stage_failure_analyzer.httpx, "AsyncClient", lambda **kw: _CapturingClient())

    many_logs = [f"line-{i}" for i in range(100)]
    asyncio.run(
        generate_stage_failure_rca(run_id="run-1", failed_stage="test", error_message="err", recent_logs=many_logs)
    )

    assert "line-0" not in captured["prompt"]
    assert "line-99" in captured["prompt"]
