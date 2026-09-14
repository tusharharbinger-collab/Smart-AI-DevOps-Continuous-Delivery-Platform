"""
tests/adversarial/test_project_deletion_cascade.py

Real bug found live: DELETE /api/v1/projects/{id} deletes from `projects`,
documented as cascading to `pipeline_executions` and everything scoped to
a run — but `verification_records`, `audit_ledger`, `approvals`, and
`cost_analysis` never had `ON DELETE CASCADE` on their `pipeline_run_id`
foreign key (unlike `execution_state`/`stage_logs`, which did). Deleting
ANY project that had ever produced a real verdict failed outright with a
500 (ForeignKeyViolationError), leaving the project half-deleted. Only
caught by actually deleting a project with real run history — nothing
exercised delete_project against one before. This is a live integration
test (like test_auth_hardening_live.py) because the failure mode is a
cross-table cascade that only a real Postgres constraint can prove.

Uses the seeded demo account (README.md Quick Start):
  demo@acme-corp.test / acme-demo-2026 (platform-admin, tenant acme-corp)
"""
import httpx
import pytest

API_BASE_URL = "http://localhost:8000"


def _api_reachable() -> bool:
    try:
        return httpx.get(f"{API_BASE_URL}/healthz", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _api_reachable(), reason="api-gateway is not running (docker compose up -d)")


def _login() -> str:
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/login",
        json={"email": "demo@acme-corp.test", "password": "acme-demo-2026"},
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def test_deleting_a_project_with_a_real_verdict_and_audit_record_does_not_500():
    token = _login()
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = httpx.post(
        f"{API_BASE_URL}/api/v1/projects",
        headers=headers,
        json={
            "name": "delete-cascade-regression-test",
            "source_type": "existing_image",
            "container_image": "localhost:5001/payments",
            "active_production_tag": "v1.1.0",
            "canary_tag": "v1.1.0",
            "traffic_steps": [100],
            "provision_cluster": False,  # cluster state is irrelevant to this DB-level regression
        },
        timeout=15.0,
    )
    assert create_resp.status_code == 200, create_resp.text
    project_id = create_resp.json()["project_id"]

    try:
        rollout_resp = httpx.post(
            f"{API_BASE_URL}/api/v1/projects/{project_id}/rollout",
            headers=headers,
            json={"commit_message": "produce a real verdict + audit record for the delete-cascade test"},
            timeout=15.0,
        )
        assert rollout_resp.status_code == 200, rollout_resp.text

        # Give the async pipeline a moment to produce a verdict and an
        # actuation (this pipeline has no build/test stage, so it's fast).
        import time

        time.sleep(3)
    finally:
        delete_resp = httpx.delete(f"{API_BASE_URL}/api/v1/projects/{project_id}", headers=headers, timeout=15.0)
        assert delete_resp.status_code == 200, (
            f"deleting a project with real run history must not 500 "
            f"(the exact bug found live): got {delete_resp.status_code} {delete_resp.text}"
        )
        assert delete_resp.json()["deleted"] == project_id
