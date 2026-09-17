"""
shared/live_url_check.py

Real gap found live: the platform could report a project's `live_url`
(`_live_url()` in api-gateway's projects_router.py) without ever actually
requesting it — "the ALB weight was flipped" and "a real visitor gets a
working page" were silently treated as the same thing. They aren't: a
target-group health check hits the ALB directly and bypasses the ALB's own
path-prefix routing entirely (documented trap — see CLAUDE.md), so a
service can report perfectly healthy targets while the real public URL
404s through the shared ALB's path-prefix forwarding.

This is the one genuine end-to-end check: a real HTTP GET against the real
public URL, going through the same ALB routing a real visitor's browser
would. Used by pipeline-worker (blue-green cutover, first-deployment
cutover) and policy-controller (canary graduation) — the one place this
logic lives so both services check "is it actually live" the same way,
matching this codebase's existing "one source of truth for actuation-
adjacent logic" discipline (see aws_ecs_actuation.py's own module
docstring).
"""
import time

import requests
import structlog

logger = structlog.get_logger(__name__)


def verify_live_url(
    url: str, retries: int = 5, delay_seconds: float = 3.0, timeout_seconds: float = 10.0
) -> dict:
    """
    Retries because a just-cutover target can take a few seconds to start
    actually serving through the ALB even after `describe_target_health`
    reports "healthy" (connection draining on the old side, DNS/ALB
    propagation) — this is deliberately a separate, slightly-delayed check
    from `wait_for_target_group_healthy`, not a duplicate of it.

    Any status code under 500 counts as "verified" — a 404 or 401 still
    proves the ALB routed the request to a real, responding application
    (which is what this function exists to confirm); it is not this
    function's job to judge whether that response is the RIGHT page, only
    that something real answered instead of the ALB's own default 404/503
    or a connection failure.
    """
    if not url:
        return {"verified": False, "status_code": None, "error": "No live_url to verify."}

    last_error: str | None = None
    last_status: int | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout_seconds, allow_redirects=True)
            last_status = resp.status_code
            if resp.status_code < 500:
                logger.info("live_url_verified", url=url, status_code=resp.status_code, attempt=attempt)
                return {"verified": True, "status_code": resp.status_code, "error": None}
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_error = str(e)

        if attempt < retries:
            time.sleep(delay_seconds)

    logger.warning("live_url_verification_failed", url=url, status_code=last_status, error=last_error, retries=retries)
    return {"verified": False, "status_code": last_status, "error": last_error}
