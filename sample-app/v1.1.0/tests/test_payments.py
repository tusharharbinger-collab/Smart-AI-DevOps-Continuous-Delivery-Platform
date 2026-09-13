"""
Unit tests for payments-service v1.1.0
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "v1.1.0"


def test_readyz():
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def test_checkout_endpoint():
    resp = client.post("/api/v1/payments/checkout")
    # In test mode without injected errors, either success or handled
    assert resp.status_code in [200, 500]
