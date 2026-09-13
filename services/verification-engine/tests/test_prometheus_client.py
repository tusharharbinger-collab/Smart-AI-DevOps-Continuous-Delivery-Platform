"""
services/verification-engine/tests/test_prometheus_client.py
"""
import numpy as np
import pytest

from src.telemetry import prometheus_client as pc


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _success(result):
    return {"status": "success", "data": {"result": result}}


def test_fetch_counter_totals_sums_all_series(monkeypatch):
    monkeypatch.setattr(
        pc.httpx, "get",
        lambda url, params, timeout: _FakeResponse(_success([
            {"metric": {}, "value": [1700000000, "12.0"]},
        ])),
    )
    total = pc.fetch_counter_totals('payments_requests_total{outcome="error"}', window_seconds=60)
    assert total == 12


def test_fetch_counter_totals_returns_zero_on_empty_result(monkeypatch):
    monkeypatch.setattr(pc.httpx, "get", lambda url, params, timeout: _FakeResponse(_success([])))
    total = pc.fetch_counter_totals("nonexistent_metric", window_seconds=60)
    assert total == 0


def test_fetch_counter_totals_raises_on_http_error(monkeypatch):
    def _raise(*args, **kwargs):
        import httpx as real_httpx
        raise real_httpx.ConnectError("connection refused")

    monkeypatch.setattr(pc.httpx, "get", _raise)
    with pytest.raises(pc.PrometheusQueryError):
        pc.fetch_counter_totals("anything", window_seconds=60)


def test_fetch_histogram_samples_reconstructs_from_buckets(monkeypatch):
    """
    3 observations in (0, 0.01], 2 in (0.01, 0.02], 0 above — cumulative
    bucket counts are 3, 5, 5 (+Inf). Reconstructed samples should be
    [0.01, 0.01, 0.01, 0.02, 0.02] (each observation attributed to its
    bucket's upper edge).
    """
    result = [
        {"metric": {"le": "0.01"}, "value": [0, "3"]},
        {"metric": {"le": "0.02"}, "value": [0, "5"]},
        {"metric": {"le": "+Inf"}, "value": [0, "5"]},
    ]
    monkeypatch.setattr(pc.httpx, "get", lambda url, params, timeout: _FakeResponse(_success(result)))

    samples = pc.fetch_histogram_samples("payments_request_duration_seconds_bucket", window_seconds=60)
    assert sorted(samples.tolist()) == [0.01, 0.01, 0.01, 0.02, 0.02]


def test_fetch_histogram_samples_handles_unordered_bucket_response(monkeypatch):
    """Prometheus doesn't guarantee series order — must sort by `le` before reconstructing."""
    result = [
        {"metric": {"le": "+Inf"}, "value": [0, "4"]},
        {"metric": {"le": "0.02"}, "value": [0, "4"]},
        {"metric": {"le": "0.01"}, "value": [0, "1"]},
    ]
    monkeypatch.setattr(pc.httpx, "get", lambda url, params, timeout: _FakeResponse(_success(result)))

    samples = pc.fetch_histogram_samples("some_bucket_metric", window_seconds=60)
    assert sorted(samples.tolist()) == [0.01, 0.02, 0.02, 0.02]


def test_fetch_histogram_samples_empty_result_returns_empty_array(monkeypatch):
    monkeypatch.setattr(pc.httpx, "get", lambda url, params, timeout: _FakeResponse(_success([])))
    samples = pc.fetch_histogram_samples("no_data_metric", window_seconds=60)
    assert samples.size == 0


def test_fetch_success_total_counts(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params, timeout):
        calls["n"] += 1
        # First call = success query, second = total query
        value = "98" if calls["n"] == 1 else "100"
        return _FakeResponse(_success([{"metric": {}, "value": [0, value]}]))

    monkeypatch.setattr(pc.httpx, "get", fake_get)
    success, total = pc.fetch_success_total_counts("success_query", "total_query", window_seconds=60)
    assert (success, total) == (98, 100)
