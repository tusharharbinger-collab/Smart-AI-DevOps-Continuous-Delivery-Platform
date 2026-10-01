"""
services/pipeline-worker/tests/test_alb_dns_publish_ttl.py

Real bug found live (2026-09-30): `_resolve_alb_base_url` published the shared ALB's real DNS name to Redis
with only a 1-hour TTL — every project's "Live · verified" badge on the Overview grid silently disappeared
an hour after its last deploy (the ALB itself never stopped serving traffic; only this cached DNS-name copy
expired, so api-gateway's boto3-free `_live_url()` fell back to the always-empty static env var and got
None). The ALB's DNS name does not change between ordinary deploys — a long TTL is a safety net against a
permanently abandoned ALB, not a real staleness risk, since a genuine ALB recreation re-runs this exact
function and overwrites the key immediately.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.worker as worker_module


class _FakeRedis:
    def __init__(self):
        self.calls: list[tuple] = []

    def set(self, key, value, ex=None, nx=None):
        self.calls.append((key, value, ex))
        return True


def test_alb_dns_name_is_published_with_a_week_long_ttl_not_one_hour(monkeypatch):
    monkeypatch.setattr(worker_module, "resolve_alb_dns_name", lambda region, name: "alb-real.us-east-1.elb.amazonaws.com")
    redis = _FakeRedis()

    result = worker_module._resolve_alb_base_url("us-east-1", redis)

    assert result == "alb-real.us-east-1.elb.amazonaws.com"
    assert redis.calls == [("platform:alb_dns_name:us-east-1", "alb-real.us-east-1.elb.amazonaws.com", 604800)]


def test_ttl_is_at_least_a_day_so_a_normal_quiet_period_never_hides_a_still_live_url(monkeypatch):
    # The exact live incident: no new deploy for over an hour, but the app was still genuinely live and
    # serving real traffic the whole time - only the cached DNS name (not the app) should ever be able to expire.
    monkeypatch.setattr(worker_module, "resolve_alb_dns_name", lambda region, name: "alb-real.us-east-1.elb.amazonaws.com")
    redis = _FakeRedis()

    worker_module._resolve_alb_base_url("us-east-1", redis)

    published_ttl = redis.calls[0][2]
    assert published_ttl >= 86400
