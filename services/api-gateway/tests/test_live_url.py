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

import asyncio

from src.routers.projects_router import _live_url as _live_url_async, GATEWAY_BASE_URL, AWS_ALB_BASE_URL


def _live_url(*a, **k):
    return asyncio.run(_live_url_async(*a, **k))


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
    assert asyncio.run(projects_router._live_url("/api/v1/widget", deploy_target="aws_ecs")) is None


def test_live_url_for_aws_ecs_reads_a_freshly_published_dns_name_from_redis():
    """BACKLOG P2 #7a - the static env var goes stale the moment the ALB is recreated; pipeline-worker/
    policy-controller publish the live DNS name to Redis, and this is where the display link must read it from."""
    class _Redis:
        async def get(self, key):
            assert key == "platform:alb_dns_name:us-east-1"
            return "fresh-alb.us-east-1.elb.amazonaws.com"

    out = asyncio.run(_live_url_async("/api/v1/widget", deploy_target="aws_ecs", redis_client=_Redis(), region="us-east-1"))
    assert out == "http://fresh-alb.us-east-1.elb.amazonaws.com/api/v1/widget/"


def test_live_url_for_aws_ecs_falls_back_to_the_env_var_when_nothing_is_published(monkeypatch):
    import src.routers.projects_router as projects_router

    monkeypatch.setattr(projects_router, "AWS_ALB_BASE_URL", "old-static-alb.example.com")

    class _Redis:
        async def get(self, key):
            return None

    out = asyncio.run(_live_url_async("/api/v1/widget", deploy_target="aws_ecs", redis_client=_Redis()))
    assert out == "http://old-static-alb.example.com/api/v1/widget/"
