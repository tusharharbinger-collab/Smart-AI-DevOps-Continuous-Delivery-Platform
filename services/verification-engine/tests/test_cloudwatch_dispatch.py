"""
services/verification-engine/tests/test_cloudwatch_dispatch.py

Covers src/main.py's _fetch_metric_from_cloudwatch — the per-category
dispatch that mutates baseline_telemetry/canary_telemetry in place from
real CloudWatch data, and the /verify endpoint's use_cloudwatch branch
(422 validation only; the happy path is exercised by
test_cloudwatch_client.py + engine.py's own existing test coverage,
which never needs to know telemetry came from CloudWatch at all).
"""
import os
import sys

# src/main.py does `from shared.logging_config import ...` — shared/ is a repo-root
# package mounted into every container at /app/shared, but isn't on this test's
# sys.path when pytest is run from services/verification-engine (the only path
# conftest.py adds). No test_main.py existed before this file for exactly this
# reason; add the repo root here rather than touching conftest.py's shared setup.
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.main import _fetch_metric_from_cloudwatch, app
from src.telemetry.cloudwatch_client import CloudWatchQueryError


def test_error_rate_dispatch_populates_requests_and_errors_keys(monkeypatch):
    from src import main

    def fake_fetch(region, service, cohort, window_seconds):
        return {"requests": [1, 2, 3], "errors": [True, False, False]}

    monkeypatch.setattr(main.cw, "fetch_error_rate_telemetry", fake_fetch)

    baseline, canary = {}, {}
    _fetch_metric_from_cloudwatch(
        {"name": "err", "category": "error_rate", "cloudwatch": {}}, 120.0, baseline, canary,
        "us-east-1", "checkout", "smartcd-platform",
    )

    assert baseline["err_requests"] == [1, 2, 3]
    assert baseline["err_errors"] == [True, False, False]
    assert canary["err_requests"] == [1, 2, 3]
    assert canary["err_errors"] == [True, False, False]


def test_latency_dispatch_populates_plain_metric_key(monkeypatch):
    from src import main

    monkeypatch.setattr(main.cw, "fetch_latency_samples", lambda region, service, cohort, w: np.array([0.05, 0.06]))

    baseline, canary = {}, {}
    _fetch_metric_from_cloudwatch(
        {"name": "p99", "category": "latency", "cloudwatch": {}}, 120.0, baseline, canary,
        "us-east-1", "checkout", "smartcd-platform",
    )

    assert baseline["p99"] == [0.05, 0.06]
    assert canary["p99"] == [0.05, 0.06]


def test_saturation_dispatch_respects_custom_metric_name(monkeypatch):
    from src import main

    captured = []

    def fake_saturation(region, service, cohort, w, metric_name, cluster):
        captured.append(metric_name)
        return np.array([40.0])

    monkeypatch.setattr(main.cw, "fetch_saturation_samples", fake_saturation)

    baseline, canary = {}, {}
    _fetch_metric_from_cloudwatch(
        {"name": "mem", "category": "saturation", "cloudwatch": {"metric_name": "MemoryUtilization"}},
        120.0, baseline, canary, "us-east-1", "checkout", "smartcd-platform",
    )

    assert captured == ["MemoryUtilization", "MemoryUtilization"]
    assert baseline["mem"] == [40.0]


def test_saturation_dispatch_defaults_to_cpu_utilization(monkeypatch):
    from src import main

    captured = []
    monkeypatch.setattr(
        main.cw, "fetch_saturation_samples",
        lambda region, service, cohort, w, metric_name, cluster: (captured.append(metric_name), np.array([1.0]))[1],
    )

    _fetch_metric_from_cloudwatch(
        {"name": "cpu", "category": "saturation", "cloudwatch": {}}, 120.0, {}, {},
        "us-east-1", "checkout", "smartcd-platform",
    )

    assert captured == ["CPUUtilization", "CPUUtilization"]


def test_business_metric_dispatch_never_fabricates_data():
    """No generic ALB/ECS-level signal exists for a business outcome —
    keys must stay unset, never a fake 0/0 or empty-but-present value that
    would look like a real (if trivial) measurement to engine.py."""
    baseline, canary = {}, {}
    _fetch_metric_from_cloudwatch(
        {"name": "checkout_rate", "category": "business_metric", "cloudwatch": {}},
        120.0, baseline, canary, "us-east-1", "checkout", "smartcd-platform",
    )

    assert baseline == {}
    assert canary == {}


def test_missing_cloudwatch_config_skips_metric_without_crashing():
    baseline, canary = {}, {}
    _fetch_metric_from_cloudwatch(
        {"name": "err", "category": "error_rate"}, 120.0, baseline, canary,
        "us-east-1", "checkout", "smartcd-platform",
    )

    assert baseline == {}
    assert canary == {}


def test_cloudwatch_query_error_degrades_gracefully_not_crash(monkeypatch):
    """Mirrors _fetch_metric_from_prometheus's own PrometheusQueryError
    handling — a transient AWS API failure must leave telemetry keys
    unset (degrading confidence downstream), never raise out of dispatch."""
    from src import main

    def raise_error(*a, **kw):
        raise CloudWatchQueryError("AccessDenied calling GetMetricData")

    monkeypatch.setattr(main.cw, "fetch_latency_samples", raise_error)

    baseline, canary = {}, {}
    _fetch_metric_from_cloudwatch(
        {"name": "p99", "category": "latency", "cloudwatch": {}}, 120.0, baseline, canary,
        "us-east-1", "checkout", "smartcd-platform",
    )

    assert baseline == {}
    assert canary == {}


def test_verify_endpoint_422s_when_cloudwatch_requested_without_region_or_service():
    client = TestClient(app)
    resp = client.post(
        "/verify",
        json={
            "pipeline_run_id": "run-1",
            "metrics": [{"name": "err", "category": "error_rate", "cloudwatch": {}}],
            "use_cloudwatch": True,
        },
    )
    assert resp.status_code == 422
    assert "aws_region" in resp.json()["detail"]
