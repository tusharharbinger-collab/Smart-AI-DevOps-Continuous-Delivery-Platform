"""
services/explainability-service/tests/test_report_generator_multimodal.py

Verifies multimodal RCA report generation:
- Schema supports root_cause_file, line_number, error_log_snippet, suggested_patch
- Fallback returns safe valid defaults adhering to the contract
- Successful Groq response with code patch validates properly
"""
import asyncio
import json
import pytest
from src.report_generator import generate_rca, _fallback_rca, RCAReport


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


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


def test_fallback_rca_includes_multimodal_fields():
    analysis_data = {
        "final_verdict": "FAILED",
        "service_name": "checkout-service",
        "commit_sha": "a1b2c3d4e5",
        "error_logs": ["[ERROR] ZeroDivisionError in line 42"],
        "metric_evidence": [{"metric_name": "error_rate", "citation": "SPRT boundary breached"}],
        "policy_checks": ["gate1_error_rate_sprt"],
    }
    result = _fallback_rca(analysis_data)
    assert result["root_cause_file"] is None
    assert result["line_number"] is None
    assert result["suspect_commit"] == "a1b2c3d4e5"
    assert result["error_log_snippet"] == "[ERROR] ZeroDivisionError in line 42"
    assert result["suggested_patch"] is None
    assert "checkout-service" in result["executive_summary"]


def test_rca_report_validation_with_patch():
    json_payload = """{
        "executive_summary": "Error rate surged past 5% due to an unhandled ZeroDivisionError.",
        "root_cause_file": "app/services/checkout.py",
        "line_number": 42,
        "suspect_commit": "a1b2c3d",
        "error_log_snippet": "ZeroDivisionError: division by zero in checkout.py line 42",
        "suggested_patch": "--- a/app/services/checkout.py\\n+++ b/app/services/checkout.py\\n@@ -42,1 +42,1 @@\\n-    rate = count / total\\n+    rate = count / total if total else 0.0\\n",
        "suggested_remediation": "Guard division by zero when total is 0.",
        "triggering_metrics": [{"metric_name": "error_rate", "citation": "SPRT tripped"}],
        "policy_clauses_evaluated": ["tier1_error_rate"]
    }"""
    report = RCAReport.model_validate_json(json_payload)
    data = report.model_dump()
    assert data["root_cause_file"] == "app/services/checkout.py"
    assert data["line_number"] == 42
    assert "suggested_patch" in data
    assert "+    rate = count / total if total else 0.0" in data["suggested_patch"]


def test_generate_rca_with_groq_response(monkeypatch):
    mock_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "executive_summary": "Canary traffic failed Wald SPRT after latency spike.",
                        "root_cause_file": "src/handler.py",
                        "line_number": 105,
                        "suspect_commit": "commit123",
                        "error_log_snippet": "[500] Database timeout",
                        "suggested_patch": "--- a/src/handler.py\n+++ b/src/handler.py\n@@ -105,1 +105,1 @@\n- timeout = 0.1\n+ timeout = 5.0\n",
                        "suggested_remediation": "Increase database timeout limit.",
                        "triggering_metrics": [],
                        "policy_clauses_evaluated": []
                    })
                }
            }
        ]
    }
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(response=_FakeResponse(mock_payload))
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    res = asyncio.run(generate_rca({"final_verdict": "FAILED"}))
    assert res["root_cause_file"] == "src/handler.py"
    assert res["line_number"] == 105
    assert res["suggested_patch"] is not None
    assert "+ timeout = 5.0" in res["suggested_patch"]
