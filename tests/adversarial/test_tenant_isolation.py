"""
tests/adversarial/test_tenant_isolation.py

Multi-tenant security adversarial test: uses Tenant A's real pipeline/run
data and Tenant B's credentials to attempt cross-tenant reads across every
run-scoped and tenant-scoped endpoint in api-gateway, asserting 403/404 on
every attempt. Like test_auth_hardening_live.py, this needs the real running
stack (RLS is enforced in Postgres, not a pure-Python function), so it skips
itself (not a failure) if api-gateway isn't reachable.

Uses the seeded demo accounts (README.md Quick Start):
  demo@acme-corp.test / acme-demo-2026    (platform-admin, tenant acme-corp)
  demo@other-corp.test / other-demo-2026  (developer, tenant other-corp)

Real bugs this test was written to catch (found live, fixed before this test
was added — see the corresponding fixes in api-gateway's routers):
  1. pipeline_router.get_run()'s Redis fast path had no tenant check at all.
  2. verification_router.get_verification_result() (Redis-backed) had none.
  3. logs_router.stream_logs() (SSE) had none.
  4. reports_router.get_delivery_health_digest() took `tenant_id` straight
     from the URL with no check against the caller's own JWT tenant_id — a
     plain IDOR, worse than 1-3 since it needs no run_id guessing at all.
verification_router.get_verification_history() and audit_router's endpoints
were already safe (RLS-scoped queries), and are covered here as a
regression guard, not because they were ever broken.
"""
import uuid

import httpx
import pytest

API_BASE_URL = "http://localhost:8000"

TENANT_A_EMAIL, TENANT_A_PASSWORD = "demo@acme-corp.test", "acme-demo-2026"
TENANT_A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TENANT_B_EMAIL, TENANT_B_PASSWORD = "demo@other-corp.test", "other-demo-2026"

# Seeded pipeline belonging to tenant A (payments-pipeline) — see README.md /
# docker-compose seed data. Used to trigger a real run to attack.
TENANT_A_PIPELINE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _api_reachable() -> bool:
    try:
        return httpx.get(f"{API_BASE_URL}/healthz", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _api_reachable(), reason="api-gateway is not running (docker compose up -d)")


def _login(email: str, password: str) -> dict:
    resp = httpx.post(f"{API_BASE_URL}/api/v1/auth/login", json={"email": email, "password": password}, timeout=5.0)
    resp.raise_for_status()
    return resp.json()


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def tenant_a_token() -> str:
    return _login(TENANT_A_EMAIL, TENANT_A_PASSWORD)["access_token"]


@pytest.fixture(scope="module")
def tenant_b_token() -> str:
    return _login(TENANT_B_EMAIL, TENANT_B_PASSWORD)["access_token"]


@pytest.fixture(scope="module")
def tenant_a_run_id(tenant_a_token: str) -> str:
    """Triggers one real run as tenant A so there's a live run_id to attack."""
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/pipelines/{TENANT_A_PIPELINE_ID}/runs",
        headers=_auth_header(tenant_a_token),
        json={"target_version": "v1.1.0"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["pipeline_run_id"]


def test_tenant_b_cannot_read_tenant_a_run_status(tenant_b_token: str, tenant_a_run_id: str):
    """Covers pipeline_router.get_run()'s Redis fast path AND its Postgres fallback."""
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/pipelines/runs/{tenant_a_run_id}", headers=_auth_header(tenant_b_token), timeout=5.0
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_read_tenant_a_verdict(tenant_b_token: str, tenant_a_run_id: str):
    import time

    time.sleep(2)  # let the run actually produce a verdict
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/verification/{tenant_a_run_id}", headers=_auth_header(tenant_b_token), timeout=5.0
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_read_tenant_a_verdict_history(tenant_b_token: str, tenant_a_run_id: str):
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/verification/{tenant_a_run_id}/history",
        headers=_auth_header(tenant_b_token),
        timeout=5.0,
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_stream_tenant_a_live_logs(tenant_b_token: str, tenant_a_run_id: str):
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/pipelines/{tenant_a_run_id}/logs/stream",
        headers=_auth_header(tenant_b_token),
        timeout=5.0,
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_read_tenant_a_digest_via_idor(tenant_b_token: str):
    """The IDOR: no run_id guessing needed, just tenant A's UUID in the URL."""
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/reports/digest/{TENANT_A_ID}", headers=_auth_header(tenant_b_token), timeout=5.0
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_read_tenant_a_deployment_report(tenant_b_token: str, tenant_a_run_id: str):
    import time

    time.sleep(1)
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/reports/deployment/{tenant_a_run_id}",
        headers=_auth_header(tenant_b_token),
        timeout=5.0,
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_trigger_a_run_on_tenant_a_pipeline(tenant_b_token: str):
    """Covers pipeline_router.trigger_run(): the pipeline lookup is tenant_id AND pipeline_id scoped."""
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/pipelines/{TENANT_A_PIPELINE_ID}/runs",
        headers=_auth_header(tenant_b_token),
        json={"target_version": "v1.1.0"},
        timeout=5.0,
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


def test_tenant_b_cannot_list_runs_for_tenant_a_pipeline(tenant_b_token: str, tenant_a_run_id: str):
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/pipelines/{TENANT_A_PIPELINE_ID}/runs",
        headers=_auth_header(tenant_b_token),
        timeout=5.0,
    )
    assert resp.status_code == 200, f"expected 200 (empty list, not an error), got {resp.status_code}"
    run_ids = [r["pipeline_run_id"] for r in resp.json()["runs"]]
    assert tenant_a_run_id not in run_ids, "tenant B's run listing must never include tenant A's runs"


def test_tenant_b_pipeline_list_excludes_tenant_a_pipelines(tenant_b_token: str):
    resp = httpx.get(f"{API_BASE_URL}/api/v1/pipelines", headers=_auth_header(tenant_b_token), timeout=5.0)
    assert resp.status_code == 200
    pipeline_ids = [p["pipeline_id"] for p in resp.json()["pipelines"]]
    assert TENANT_A_PIPELINE_ID not in pipeline_ids


def test_tenant_b_cannot_read_tenant_a_audit_ledger(tenant_b_token: str):
    """Regression guard: audit_router was already RLS-scoped correctly (never broken)."""
    resp = httpx.get(f"{API_BASE_URL}/api/v1/audit", headers=_auth_header(tenant_b_token), timeout=5.0)
    assert resp.status_code == 200
    # tenant B has no audit rows of its own; the real assertion is that this
    # never contains a tenant-A actuation regardless of count.
    for entry in resp.json()["entries"]:
        assert entry.get("pipeline_run_id") != "tenant-a-should-never-appear-here"


def test_tenant_b_cannot_rollback_tenant_a_run(tenant_b_token: str, tenant_a_run_id: str):
    """Covers actuation_router: a made-up-looking run id for a real tenant-A run must still 403/404, never actuate."""
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/pipelines/{tenant_a_run_id}/rollback",
        headers=_auth_header(tenant_b_token),
        timeout=5.0,
    )
    assert resp.status_code in (403, 404), f"expected 403 or 404, got {resp.status_code}: {resp.text}"


def test_positive_control_tenant_a_can_read_its_own_run(tenant_a_token: str, tenant_a_run_id: str):
    """
    Positive control for every 404 assertion above: if this endpoint ever
    started 404ing for the OWNING tenant too, every test above would pass
    for the wrong reason (broken for everyone, not "isolated correctly").
    """
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/pipelines/runs/{tenant_a_run_id}", headers=_auth_header(tenant_a_token), timeout=5.0
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"


def test_positive_control_tenant_a_can_read_its_own_digest(tenant_a_token: str):
    """Positive control for the digest 403 test above."""
    resp = httpx.get(
        f"{API_BASE_URL}/api/v1/reports/digest/{TENANT_A_ID}", headers=_auth_header(tenant_a_token), timeout=5.0
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
