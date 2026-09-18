"""
services/explainability-service/tests/test_chatops_answerer.py

Mirrors test_digest_summarizer.py's mocking approach exactly — same
non-negotiable guarantee: a Groq outage degrades to the deterministic
fallback rather than ever raising. Two additional cases specific to
ChatOps: a project with zero run history must get the honest "nothing to
answer from" fallback rather than a hallucinated one, and a real answer
must carry through its cited_run_ids/confidence fields untouched.
"""
import asyncio
import json

import httpx

import src.chatops_answerer as chatops_answerer
from src.chatops_answerer import answer_chatops_question


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _groq_payload(answer: str, cited_run_ids: list[str], confidence: str) -> dict:
    return {
        "choices": [
            {"message": {"content": json.dumps({"answer": answer, "cited_run_ids": cited_run_ids, "confidence": confidence})}}
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


_CONTEXT_WITH_RUNS = {
    "project_id": "proj-1",
    "runs": [
        {
            "pipeline_run_id": "run-abc123",
            "status": "FAILED",
            "verdict": None,
            "audit_actions": [
                {"action": "BLUE_GREEN_CUTOVER", "authorized_by": "OPA:rule=HEALTH_GATED_CUTOVER"},
                {"action": "ROLLBACK", "authorized_by": "SYSTEM:blue_green_post_cutover_live_url_verification_failed"},
            ],
        }
    ],
}

_CONTEXT_NO_RUNS = {"project_id": "proj-empty", "runs": []}


def test_returns_fallback_when_groq_api_key_not_configured(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = asyncio.run(answer_chatops_question(_CONTEXT_WITH_RUNS, "why did the last deploy roll back?"))

    assert result["confidence"] == "insufficient_data"
    assert result["cited_run_ids"] == []


def test_zero_run_history_falls_back_to_the_honest_no_data_message(monkeypatch):
    # The empty-run check lives in the fallback path (used on any
    # failure/timeout/no-key) rather than short-circuiting before Groq is
    # ever called — a real key with a genuinely empty project would still
    # ask Groq (which should itself say "insufficient data"), but no API
    # key means the fallback fires directly and must give the honest
    # zero-run message, not the generic "temporarily unavailable" one.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = asyncio.run(answer_chatops_question(_CONTEXT_NO_RUNS, "why did the last deploy roll back?"))

    assert result["confidence"] == "insufficient_data"
    assert "no recorded runs" in result["answer"]


def test_returns_real_grounded_answer_from_a_successful_groq_call(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(
        _FakeResponse(_groq_payload("Run abc123 rolled back because the post-cutover live-URL check failed.", ["run-abc123"], "grounded"))
    )
    monkeypatch.setattr(chatops_answerer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(answer_chatops_question(_CONTEXT_WITH_RUNS, "why did the last deploy roll back?"))

    assert "abc123" in result["answer"]
    assert result["cited_run_ids"] == ["run-abc123"]
    assert result["confidence"] == "grounded"


def test_falls_back_on_groq_timeout_never_raises(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(exc=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(chatops_answerer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(answer_chatops_question(_CONTEXT_WITH_RUNS, "why did the last deploy roll back?"))

    assert result["confidence"] == "insufficient_data"


def test_falls_back_on_malformed_groq_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse({"choices": [{"message": {"content": "not valid json"}}]}))
    monkeypatch.setattr(chatops_answerer.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(answer_chatops_question(_CONTEXT_WITH_RUNS, "why did the last deploy roll back?"))

    assert result["confidence"] == "insufficient_data"
