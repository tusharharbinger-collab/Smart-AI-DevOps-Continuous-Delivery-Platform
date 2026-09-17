"""
services/api-gateway/tests/test_live_url.py

Real gap found live: a project's path_prefix was computed at onboarding,
routed into the real generated HTTPRoute, and then thrown away — never
persisted on the projects row, never returned by any API. There was no way
for a user to click a link and check "is my product truly live" the way
Render/Vercel do. Covers `_live_url`, the pure helper `list_projects` and
`_load_project`/`get_project` now use to compute it.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.routers.projects_router import _live_url, GATEWAY_BASE_URL, AWS_ALB_BASE_URL


def test_live_url_combines_gateway_base_and_path_prefix_with_a_trailing_slash():
    # Real gap found live (2026-09-17): a missing trailing slash here meant
    # a browser resolved a project's own relative asset references
    # (<link href="style.css">) against the PARENT path instead of the
    # project's own — a real onboarded app rendered completely unstyled
    # with no working JS at exactly this link. See
    # shared/live_url_check.py::build_live_url's docstring for the full
    # incident.
    assert _live_url("/api/v1/widget") == f"{GATEWAY_BASE_URL}/api/v1/widget/"


def test_live_url_does_not_double_a_trailing_slash_already_present():
    assert _live_url("/api/v1/widget/") == f"{GATEWAY_BASE_URL}/api/v1/widget/"


def test_live_url_is_none_when_no_path_prefix_is_set():
    assert _live_url(None) is None
    assert _live_url("") is None


def test_live_url_for_aws_ecs_also_has_a_trailing_slash():
    if not AWS_ALB_BASE_URL:
        return  # no default configured in this test environment — nothing to assert
    assert _live_url("/api/v1/widget", deploy_target="aws_ecs") == f"http://{AWS_ALB_BASE_URL}/api/v1/widget/"


def test_live_url_for_aws_ecs_is_none_when_alb_base_url_not_configured(monkeypatch):
    import src.routers.projects_router as projects_router

    monkeypatch.setattr(projects_router, "AWS_ALB_BASE_URL", None)
    assert projects_router._live_url("/api/v1/widget", deploy_target="aws_ecs") is None
