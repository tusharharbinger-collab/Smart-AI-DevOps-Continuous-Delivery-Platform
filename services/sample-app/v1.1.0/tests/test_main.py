"""
services/sample-app/v1.1.0/tests/test_main.py

Real gap found live (first time this pipeline's "test" stage was ever
actually executed against a real deploy target): this directory didn't
exist at all — the seeded payments-pipeline's test command
("pytest sample-app/v1.1.0/tests/ -v") always referenced a path with
nothing in it. These are genuine tests against the app's real endpoints via
FastAPI's TestClient, not a placeholder to make the stage pass.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_healthz_reports_healthy_with_version_and_cohort():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["version"] == main.APP_VERSION
    assert body["cohort"] == main.DEPLOYMENT_COHORT


def test_readyz_reports_ready():
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"ready": True}


def test_metrics_exposes_prometheus_text_format():
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "payments_requests_total" in resp.text
    assert "payments_request_duration_seconds" in resp.text


def test_checkout_returns_a_well_shaped_response_on_success_or_failure():
    """error_rate is probabilistic (0.5% normally), so this asserts the
    response shape is correct for whichever outcome actually occurred,
    rather than assuming success — a flaky assertion here would be worse
    than not testing it at all."""
    resp = client.post("/api/v1/payments/checkout")
    assert resp.status_code in (200, 500)
    body = resp.json()
    if resp.status_code == 200:
        assert body["success"] is True
        assert body["transaction_id"].startswith("txn-")
        assert body["version"] == main.APP_VERSION
    else:
        assert body["success"] is False
        assert body["error"] == "payment_processor_timeout"


def test_payment_health_reports_service_identity():
    resp = client.get("/api/v1/payments/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "payments"
    assert body["version"] == main.APP_VERSION
    assert body["cohort"] == main.DEPLOYMENT_COHORT
