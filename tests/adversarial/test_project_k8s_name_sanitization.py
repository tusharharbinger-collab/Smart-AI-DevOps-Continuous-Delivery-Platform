"""
tests/adversarial/test_project_k8s_name_sanitization.py

Real bug found live: a project named "RaktDoot" (from a real GitHub repo
of that name) passed straight through to Kubernetes Deployment/Service/
HTTPRoute names — Kubernetes requires a lowercase RFC 1123 label/subdomain
for every one of those, and rejected onboarding outright with a 422
("RaktDoot-baseline": a lowercase RFC 1123 subdomain must consist of lower
case alphanumeric characters...) that the wizard gave the user no way to
anticipate. Separately, the wizard's auto-filled container_image
("registry.internal/RaktDoot") would have failed the build stage too,
since Docker repository names must also be lowercase.

Live integration test (like test_auth_hardening_live.py) because the
actual failure mode was a real Kubernetes API rejection, not something a
pure-Python unit test of the YAML string template alone would catch —
this asserts the real generated pipeline YAML never contains an uppercase
K8s-facing identifier, and that a hand-edited-uppercase container_image is
rejected clearly at creation time instead of failing deep in the build
stage.
"""
import httpx
import pytest
import yaml

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


def test_mixed_case_project_name_generates_lowercase_k8s_identifiers():
    token = _login()
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = httpx.post(
        f"{API_BASE_URL}/api/v1/projects",
        headers=headers,
        json={
            "name": "RaktDoot-Regression-Test",
            "source_type": "existing_image",
            "container_image": "registry.internal/raktdoot-regression-test",
            "active_production_tag": "v1.0.0",
            "canary_tag": "v1.1.0",
            "traffic_steps": [100],
            "provision_cluster": False,  # only the generated YAML matters here
        },
        timeout=15.0,
    )
    assert create_resp.status_code == 200, create_resp.text
    body = create_resp.json()
    project_id = body["project_id"]

    try:
        parsed = yaml.safe_load(body["generated_pipeline_yaml"])
        cfg = parsed["spec"]["stages"][0]["config"]
        for field in ("service", "routeName", "canaryDeployment", "baselineDeployment"):
            value = cfg[field]
            assert value == value.lower(), f"{field}={value!r} must be lowercase for Kubernetes"
            assert " " not in value
    finally:
        httpx.delete(f"{API_BASE_URL}/api/v1/projects/{project_id}", headers=headers, timeout=15.0)


def test_uppercase_container_image_is_rejected_clearly():
    token = _login()
    headers = {"Authorization": f"Bearer {token}"}

    resp = httpx.post(
        f"{API_BASE_URL}/api/v1/projects",
        headers=headers,
        json={
            "name": "uppercase-image-regression-test",
            "source_type": "existing_image",
            "container_image": "registry.internal/RaktDoot",
            "active_production_tag": "v1.0.0",
            "canary_tag": "v1.1.0",
            "provision_cluster": False,
        },
        timeout=15.0,
    )

    assert resp.status_code == 422, (
        "an uppercase container_image must be rejected at creation time, not fail later mid-build: "
        f"got {resp.status_code} {resp.text}"
    )
    assert "lowercase" in resp.json()["detail"].lower()
