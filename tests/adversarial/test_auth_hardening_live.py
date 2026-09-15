"""
tests/adversarial/test_auth_hardening_live.py

Phase 4 (§04-security-hardening.md, deliverables 4.1/4.2): unlike the rest
of tests/adversarial/ (pure-Python, no running services required), these
attacks can only be attempted against the real api-gateway HTTP surface —
authorization and rate-limiting are enforced in FastAPI dependencies/
middleware, not in a library function that can be called directly. Skips
itself (not a failure) if api-gateway isn't reachable, so the rest of the
adversarial suite still runs standalone; run `docker compose up -d` first
to actually exercise these.

Uses the seeded demo accounts (README.md Quick Start):
  demo@acme-corp.test / acme-demo-2026    (platform-admin, tenant acme-corp)
  demo@other-corp.test / other-demo-2026  (developer, tenant other-corp — no pipelines)
"""
import uuid

import httpx
import pytest

API_BASE_URL = "http://localhost:8000"


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


def test_developer_role_cannot_pause_a_pipeline():
    """
    A `developer` calling an action gated to `lead-sre`+ must get 403 — not
    200, and not a silent no-op. Uses a made-up run id: if RBAC is bypassed,
    the request reaches the handler and 404s on the missing run instead
    (proving the gate didn't fire) rather than 403ing before ever looking
    the run up.
    """
    session = _login("demo@other-corp.test", "other-demo-2026")
    assert session["role"] == "developer"

    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/pipelines/{uuid.uuid4()}/pause",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        timeout=5.0,
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_developer_role_cannot_generate_a_pipeline_via_ai():
    """
    The AI pipeline-authoring endpoint is gated the same as any other
    policy-mutating action (require_role("lead-sre")) — a `developer` must
    get 403, not reach the handler (which would 404 on the made-up project
    id, proving the gate didn't fire, if RBAC were bypassed).
    """
    session = _login("demo@other-corp.test", "other-demo-2026")
    assert session["role"] == "developer"

    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/projects/{uuid.uuid4()}/pipeline/generate",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"prompt": "add a 15% canary step"},
        timeout=5.0,
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_developer_role_cannot_register_a_new_service():
    session = _login("demo@other-corp.test", "other-demo-2026")
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/services",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={
            "service_name": f"adversarial-test-{uuid.uuid4().hex[:8]}",
            "image": "localhost:5001/payments",
            "baseline_tag": "v1.0.0",
            "canary_tag": "v1.1.0",
        },
        timeout=5.0,
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_platform_admin_role_passes_the_gate():
    """
    Positive control for the two tests above: a sufficiently-privileged role
    gets PAST the RBAC gate (a 404 for a made-up run id proves the handler
    ran — a 403 here would mean the gate is misconfigured to reject everyone).
    """
    session = _login("demo@acme-corp.test", "acme-demo-2026")
    assert session["role"] == "platform-admin"

    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/pipelines/{uuid.uuid4()}/pause",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        timeout=5.0,
    )
    assert resp.status_code == 404, f"expected 404 (past the RBAC gate, run not found), got {resp.status_code}"


def test_request_with_no_token_is_rejected_before_any_role_check():
    resp = httpx.post(f"{API_BASE_URL}/api/v1/pipelines/{uuid.uuid4()}/pause", timeout=5.0)
    assert resp.status_code == 401


def test_repeated_failed_logins_trigger_a_temporary_lockout():
    """A brute-force attacker gets locked out after N failures, not an unlimited number of guesses."""
    email = f"lockout-adversarial-{uuid.uuid4().hex[:8]}@acme-corp.test"
    statuses = []
    for _ in range(7):
        resp = httpx.post(
            f"{API_BASE_URL}/api/v1/auth/login", json={"email": email, "password": "wrong-password"}, timeout=5.0
        )
        statuses.append(resp.status_code)

    assert statuses[:5] == [401] * 5, f"expected the first 5 attempts to be 401, got {statuses[:5]}"
    assert 429 in statuses[5:], f"expected a 429 lockout after 5 failures, got {statuses[5:]}"


def test_forged_refresh_token_is_rejected():
    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/refresh", json={"refresh_token": "attacker-guessed-this-token"}, timeout=5.0
    )
    assert resp.status_code == 401


def test_refresh_token_is_single_use_rotated_not_reusable():
    """A stolen (but already-used-once-by-the-legitimate-client) refresh token must not work a second time."""
    session = _login("demo@acme-corp.test", "acme-demo-2026")
    original_refresh_token = session["refresh_token"]

    first_use = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/refresh", json={"refresh_token": original_refresh_token}, timeout=5.0
    )
    assert first_use.status_code == 200

    replay = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/refresh", json={"refresh_token": original_refresh_token}, timeout=5.0
    )
    assert replay.status_code == 401, "a rotated-out refresh token must not still be usable"


def test_logout_immediately_revokes_the_refresh_token():
    session = _login("demo@acme-corp.test", "acme-demo-2026")
    refresh_token = session["refresh_token"]

    logout_resp = httpx.post(f"{API_BASE_URL}/api/v1/auth/logout", json={"refresh_token": refresh_token}, timeout=5.0)
    assert logout_resp.status_code == 200

    reuse_resp = httpx.post(f"{API_BASE_URL}/api/v1/auth/refresh", json={"refresh_token": refresh_token}, timeout=5.0)
    assert reuse_resp.status_code == 401, "a logged-out refresh token must be immediately revoked"
