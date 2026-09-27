"""
shared/provisioning/alb_resolver.py

BACKLOG P2 #7a — the shared ALB's DNS name changes every time it is recreated (a new ALB, or the same name after a
teardown), but `AWS_ALB_BASE_URL` used to be a static env var nothing ever updated after the first onboarding. A
project's displayed live_url and, more importantly, the post-cutover `verify_live_url` gate then silently targeted
a dead host with no error — found live after a full account teardown recreated the ALB under a new DNS name.

This resolves the DNS name live from AWS by the ALB's stable NAME (`smartcd-platform-alb`, never its DNS name or
ARN, both of which change) every time a caller needs it, so it is always eventually consistent with reality. A
short in-process cache keeps a hot path (every canary-step reverify, every blue-green cutover) from hitting
DescribeLoadBalancers on every single call; `invalidate_alb_dns_cache` clears it immediately after the ALB is
created or recreated so the very next lookup is never stale.

Pure boto3, synchronous — callers use asyncio.to_thread, same convention as the rest of shared/provisioning/.
"""
import threading
import time

import boto3
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 120
_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_lock = threading.Lock()


def resolve_alb_dns_name(region: str, alb_name: str) -> str | None:
    """
    The real DNS name of the named ALB in this account/region right now, or None if it does not exist (not yet
    onboarded, or torn down since). Never raises for a missing ALB — a caller treats None as "not live yet",
    the same way a static env var being unset used to be handled, just always current instead of possibly stale.
    """
    key = (region, alb_name)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    try:
        elb = boto3.client("elbv2", region_name=region)
        albs = elb.describe_load_balancers(Names=[alb_name])["LoadBalancers"]
    except ClientError as e:
        if "LoadBalancerNotFound" in str(e):
            albs = []
        else:
            logger.warning("alb_dns_resolution_failed", region=region, alb_name=alb_name, error=str(e))
            raise
    dns_name = albs[0]["DNSName"] if albs else None

    with _lock:
        _cache[key] = (time.monotonic(), dns_name)
    return dns_name


def invalidate_alb_dns_cache(region: str, alb_name: str) -> None:
    """Call right after creating/recreating the ALB so the very next resolve_alb_dns_name is never stale."""
    with _lock:
        _cache.pop((region, alb_name), None)
