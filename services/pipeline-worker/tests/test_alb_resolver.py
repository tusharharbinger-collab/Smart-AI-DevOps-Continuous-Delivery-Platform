"""BACKLOG P2 #7a - resolve the shared ALB's DNS name live from AWS instead of a static env var."""
from botocore.exceptions import ClientError

from shared.provisioning import alb_resolver as m


def setup_function():
    m._cache.clear()


class _Elb:
    def __init__(self, dns=None, error=False):
        self.dns, self.error, self.calls = dns, error, 0

    def describe_load_balancers(self, Names):
        self.calls += 1
        if self.error:
            raise ClientError({"Error": {"Code": "LoadBalancerNotFoundException", "Message": "x"}}, "DescribeLoadBalancers")
        return {"LoadBalancers": [{"DNSName": self.dns}]}


def test_resolves_the_real_dns_name(monkeypatch):
    elb = _Elb(dns="alb-123.us-east-1.elb.amazonaws.com")
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: elb)
    assert m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb") == "alb-123.us-east-1.elb.amazonaws.com"


def test_a_missing_alb_returns_none_not_an_exception(monkeypatch):
    elb = _Elb(error=True)
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: elb)
    assert m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb") is None


def test_repeated_calls_within_the_ttl_hit_the_cache_not_aws(monkeypatch):
    elb = _Elb(dns="alb-1.example.com")
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: elb)
    for _ in range(5):
        m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb")
    assert elb.calls == 1


def test_a_different_region_or_name_is_a_separate_cache_entry(monkeypatch):
    elb = _Elb(dns="alb-1.example.com")
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: elb)
    m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb")
    m.resolve_alb_dns_name("eu-north-1", "smartcd-platform-alb")
    m.resolve_alb_dns_name("us-east-1", "other-alb")
    assert elb.calls == 3


def test_recreating_the_alb_is_picked_up_immediately_after_invalidation(monkeypatch):
    elb = _Elb(dns="old-dns.example.com")
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: elb)
    assert m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb") == "old-dns.example.com"
    elb.dns = "new-dns.example.com"
    m.invalidate_alb_dns_cache("us-east-1", "smartcd-platform-alb")
    assert m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb") == "new-dns.example.com"


def test_a_real_error_other_than_not_found_is_raised(monkeypatch):
    import pytest
    elb = _Elb()
    def boom(*a, **k):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "DescribeLoadBalancers")
    elb.describe_load_balancers = boom
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: elb)
    with pytest.raises(ClientError):
        m.resolve_alb_dns_name("us-east-1", "smartcd-platform-alb")
