"""
services/verification-engine/tests/test_promql_sanitizer.py

Phase 1 hardening: validates user-supplied PromQL business-metric queries
before verification is scheduled. Prometheus exposes no separate
"validate-only" endpoint, so promql_validator.py validates by actually
running the query through /api/v1/query with a 5s timeout — these tests
mock httpx.get to simulate Prometheus's real response shapes (a 400
bad_data for a syntax error; a 200 with resultType "scalar"/"string" for a
query that parses fine but isn't usable here).
"""
import httpx
import pytest

from src import promql_validator as pv


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _ok(result_type):
    return _FakeResponse(200, {"status": "success", "data": {"resultType": result_type, "result": []}})


def _bad_data(error_detail="parse error at char 5"):
    return _FakeResponse(
        400, {"status": "error", "errorType": "bad_data", "error": error_detail}
    )


def test_valid_instant_vector_query_passes(monkeypatch):
    monkeypatch.setattr(pv.httpx, "get", lambda url, params, timeout: _ok("vector"))
    pv.validate_promql_query('payments_requests_total{outcome="success"}')  # must not raise


def test_valid_range_vector_query_passes(monkeypatch):
    monkeypatch.setattr(pv.httpx, "get", lambda url, params, timeout: _ok("matrix"))
    pv.validate_promql_query("payments_requests_total[5m]")  # must not raise


def test_scalar_result_is_rejected(monkeypatch):
    monkeypatch.setattr(pv.httpx, "get", lambda url, params, timeout: _ok("scalar"))
    with pytest.raises(pv.PromQLValidationError, match="scalar"):
        pv.validate_promql_query("1 + 1")


def test_string_result_is_rejected(monkeypatch):
    monkeypatch.setattr(pv.httpx, "get", lambda url, params, timeout: _ok("string"))
    with pytest.raises(pv.PromQLValidationError):
        pv.validate_promql_query('"literal string"')


def test_syntax_error_is_rejected_with_prometheus_detail(monkeypatch):
    monkeypatch.setattr(
        pv.httpx, "get", lambda url, params, timeout: _bad_data("unexpected character inside braces: '$'")
    )
    with pytest.raises(pv.PromQLValidationError, match="unexpected character"):
        pv.validate_promql_query("payments_requests_total{outcome=$success}")


def test_empty_query_rejected_without_a_network_call(monkeypatch):
    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("must not call Prometheus for an empty query")

    monkeypatch.setattr(pv.httpx, "get", _should_not_be_called)
    with pytest.raises(pv.PromQLValidationError, match="empty"):
        pv.validate_promql_query("")


def test_query_timeout_is_wrapped_as_validation_error(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(pv.httpx, "get", _raise_timeout)
    with pytest.raises(pv.PromQLValidationError, match="timed out"):
        pv.validate_promql_query("some_metric_total")


def test_prometheus_unreachable_is_wrapped_as_validation_error(monkeypatch):
    def _raise_connect_error(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(pv.httpx, "get", _raise_connect_error)
    with pytest.raises(pv.PromQLValidationError, match="Could not reach Prometheus"):
        pv.validate_promql_query("some_metric_total")


def test_validation_uses_a_strict_five_second_timeout(monkeypatch):
    captured = {}

    def _capture_timeout(url, params, timeout):
        captured["timeout"] = timeout
        return _ok("vector")

    monkeypatch.setattr(pv.httpx, "get", _capture_timeout)
    pv.validate_promql_query("some_metric_total")
    assert captured["timeout"] == 5.0


def test_validate_metric_prometheus_config_checks_every_declared_query_field(monkeypatch):
    seen_queries = []

    def _record(url, params, timeout):
        seen_queries.append(params["query"])
        return _ok("vector")

    monkeypatch.setattr(pv.httpx, "get", _record)
    pv.validate_metric_prometheus_config(
        {
            "success_query": 'payments_requests_total{cohort="{cohort}",outcome="success"}',
            "error_query": 'payments_requests_total{cohort="{cohort}",outcome="error"}',
        }
    )
    assert len(seen_queries) == 2


def test_validate_metric_prometheus_config_reports_which_field_is_invalid(monkeypatch):
    monkeypatch.setattr(pv.httpx, "get", lambda url, params, timeout: _bad_data("bad selector"))
    with pytest.raises(pv.PromQLValidationError, match="error_query"):
        pv.validate_metric_prometheus_config({"error_query": "malformed{{query"})


def test_validate_metric_prometheus_config_ignores_unset_fields(monkeypatch):
    """A metric that only declares success_query/error_query (error_rate) must not be
    validated against histogram_query/gauge_query/total_query, which it never set."""

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("must not validate a field the metric never declared")

    monkeypatch.setattr(pv.httpx, "get", _should_not_be_called)
    pv.validate_metric_prometheus_config({})  # no query fields at all — must not raise or call out
