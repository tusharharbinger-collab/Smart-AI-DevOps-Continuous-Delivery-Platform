"""
services/verification-engine/tests/test_cloudwatch_client.py

Mirrors test_prometheus_client.py's mocking approach (monkeypatch the
boto3 client factory directly). Real gap this closes: AWS ECS-deployed
projects have verified against synthetic fallback telemetry since the ECS
target went live — nothing ever queried real ALB/ECS CloudWatch metrics.
"""
import numpy as np
import pytest

from src.telemetry import cloudwatch_client as cwc


class _FakeElbv2:
    def __init__(self, load_balancers=None, target_groups=None, raise_on=None):
        self._lbs = load_balancers or {}
        self._tgs = target_groups or {}
        self._raise_on = raise_on or set()

    def describe_load_balancers(self, Names):
        name = Names[0]
        if name in self._raise_on:
            raise RuntimeError(f"load balancer '{name}' not found")
        return {"LoadBalancers": self._lbs.get(name, [])}

    def describe_target_groups(self, Names):
        name = Names[0]
        if name in self._raise_on:
            raise RuntimeError(f"target group '{name}' not found")
        return {"TargetGroups": self._tgs.get(name, [])}


class _FakeCloudWatch:
    def __init__(self, values=None, raise_error=None):
        self._values = values if values is not None else []
        self._raise_error = raise_error
        self.last_call = None

    def get_metric_data(self, **kwargs):
        self.last_call = kwargs
        if self._raise_error:
            raise self._raise_error
        return {"MetricDataResults": [{"Id": "m1", "Values": self._values}]}


def test_resolve_load_balancer_dimension_extracts_arn_suffix(monkeypatch):
    fake = _FakeElbv2(
        load_balancers={
            "smartcd-platform-alb": [
                {"LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:236087863083:loadbalancer/app/smartcd-platform-alb/1806553190abcdef"}
            ]
        }
    )
    monkeypatch.setattr(cwc, "_elbv2_client", lambda region: fake)

    dim = cwc.resolve_load_balancer_dimension("us-east-1")

    assert dim == "app/smartcd-platform-alb/1806553190abcdef"


def test_resolve_load_balancer_dimension_raises_when_not_found(monkeypatch):
    fake = _FakeElbv2(load_balancers={})
    monkeypatch.setattr(cwc, "_elbv2_client", lambda region: fake)

    with pytest.raises(cwc.CloudWatchQueryError):
        cwc.resolve_load_balancer_dimension("us-east-1", "nonexistent-alb")


def test_resolve_target_group_dimension_extracts_arn_suffix(monkeypatch):
    fake = _FakeElbv2(
        target_groups={
            "checkout-baseline": [
                {"TargetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:236087863083:targetgroup/checkout-baseline/abcdef1234567890"}
            ]
        }
    )
    monkeypatch.setattr(cwc, "_elbv2_client", lambda region: fake)

    dim = cwc.resolve_target_group_dimension("us-east-1", "checkout-baseline")

    assert dim == "targetgroup/checkout-baseline/abcdef1234567890"


def test_resolve_target_group_dimension_raises_on_aws_error(monkeypatch):
    fake = _FakeElbv2(raise_on={"missing-tg"})
    monkeypatch.setattr(cwc, "_elbv2_client", lambda region: fake)

    with pytest.raises(cwc.CloudWatchQueryError):
        cwc.resolve_target_group_dimension("us-east-1", "missing-tg")


def test_fetch_sum_over_window_sums_all_datapoints(monkeypatch):
    fake_cw = _FakeCloudWatch(values=[10.0, 15.0, 5.0])
    monkeypatch.setattr(cwc, "_cloudwatch_client", lambda region: fake_cw)

    total = cwc.fetch_sum_over_window("us-east-1", "AWS/ApplicationELB", "RequestCount", [], window_seconds=60)

    assert total == 30


def test_fetch_sum_over_window_empty_result_is_zero(monkeypatch):
    fake_cw = _FakeCloudWatch(values=[])
    monkeypatch.setattr(cwc, "_cloudwatch_client", lambda region: fake_cw)

    total = cwc.fetch_sum_over_window("us-east-1", "AWS/ApplicationELB", "RequestCount", [], window_seconds=60)

    assert total == 0


def test_fetch_metric_data_raises_cloudwatch_query_error_on_api_failure(monkeypatch):
    fake_cw = _FakeCloudWatch(raise_error=RuntimeError("AccessDenied"))
    monkeypatch.setattr(cwc, "_cloudwatch_client", lambda region: fake_cw)

    with pytest.raises(cwc.CloudWatchQueryError):
        cwc.fetch_sum_over_window("us-east-1", "AWS/ApplicationELB", "RequestCount", [], window_seconds=60)


def test_fetch_metric_samples_returns_array_of_datapoints(monkeypatch):
    fake_cw = _FakeCloudWatch(values=[0.042, 0.038, 0.051])
    monkeypatch.setattr(cwc, "_cloudwatch_client", lambda region: fake_cw)

    samples = cwc.fetch_metric_samples("us-east-1", "AWS/ApplicationELB", "TargetResponseTime", [], window_seconds=120)

    assert isinstance(samples, np.ndarray)
    assert sorted(samples.tolist()) == [0.038, 0.042, 0.051]


def test_sample_period_seconds_respects_cloudwatch_minimum_and_maximum():
    assert cwc._sample_period_seconds(60) == 60  # never below CloudWatch's 60s standard-resolution floor
    assert cwc._sample_period_seconds(600) == 60
    assert cwc._sample_period_seconds(3000) == 300  # never above the 300s ceiling this module chose
    assert cwc._sample_period_seconds(1200) == 120


def test_fetch_error_rate_telemetry_builds_correct_shape(monkeypatch):
    monkeypatch.setattr(cwc, "resolve_load_balancer_dimension", lambda region: "app/alb/123")
    monkeypatch.setattr(cwc, "resolve_target_group_dimension", lambda region, name: f"targetgroup/{name}/456")

    calls = []

    def fake_sum(region, namespace, metric_name, dimensions, window_seconds):
        calls.append(metric_name)
        return 100 if metric_name == "RequestCount" else 3

    monkeypatch.setattr(cwc, "fetch_sum_over_window", fake_sum)

    result = cwc.fetch_error_rate_telemetry("us-east-1", "checkout", "canary", window_seconds=60)

    assert len(result["requests"]) == 100
    assert result["errors"].count(True) == 3
    assert result["errors"].count(False) == 97
    assert calls == ["RequestCount", "HTTPCode_Target_5XX_Count"]


def test_fetch_error_rate_telemetry_never_lets_errors_exceed_total(monkeypatch):
    """Defensive clamp — a transient cross-metric AWS inconsistency must
    never invert the ratio into something statistically nonsensical."""
    monkeypatch.setattr(cwc, "resolve_load_balancer_dimension", lambda region: "app/alb/123")
    monkeypatch.setattr(cwc, "resolve_target_group_dimension", lambda region, name: f"targetgroup/{name}/456")

    def fake_sum(region, namespace, metric_name, dimensions, window_seconds):
        return 5 if metric_name == "RequestCount" else 50  # errors > total, deliberately

    monkeypatch.setattr(cwc, "fetch_sum_over_window", fake_sum)

    result = cwc.fetch_error_rate_telemetry("us-east-1", "checkout", "baseline", window_seconds=60)

    assert result["errors"].count(True) == 5  # clamped to total
    assert result["errors"].count(False) == 0


def test_fetch_latency_samples_uses_target_response_time(monkeypatch):
    monkeypatch.setattr(cwc, "resolve_load_balancer_dimension", lambda region: "app/alb/123")
    monkeypatch.setattr(cwc, "resolve_target_group_dimension", lambda region, name: f"targetgroup/{name}/456")

    captured = {}

    def fake_samples(region, namespace, metric_name, dimensions, window_seconds, stat="Average"):
        captured.update(namespace=namespace, metric_name=metric_name, dimensions=dimensions, stat=stat)
        return np.array([0.05, 0.06])

    monkeypatch.setattr(cwc, "fetch_metric_samples", fake_samples)

    samples = cwc.fetch_latency_samples("us-east-1", "checkout", "canary", window_seconds=120)

    assert captured["namespace"] == "AWS/ApplicationELB"
    assert captured["metric_name"] == "TargetResponseTime"
    assert captured["stat"] == "Average"
    assert {"Name": "TargetGroup", "Value": "targetgroup/checkout-canary/456"} in captured["dimensions"]
    assert samples.tolist() == [0.05, 0.06]


def test_fetch_saturation_samples_uses_ecs_namespace_and_cluster_dimensions(monkeypatch):
    captured = {}

    def fake_samples(region, namespace, metric_name, dimensions, window_seconds, stat="Average"):
        captured.update(namespace=namespace, metric_name=metric_name, dimensions=dimensions)
        return np.array([45.0, 50.0])

    monkeypatch.setattr(cwc, "fetch_metric_samples", fake_samples)

    samples = cwc.fetch_saturation_samples("us-east-1", "checkout", "baseline", window_seconds=120)

    assert captured["namespace"] == "AWS/ECS"
    assert captured["metric_name"] == "CPUUtilization"
    assert {"Name": "ClusterName", "Value": cwc.DEFAULT_ECS_CLUSTER} in captured["dimensions"]
    assert {"Name": "ServiceName", "Value": "checkout-baseline"} in captured["dimensions"]
    assert samples.tolist() == [45.0, 50.0]


def test_fetch_saturation_samples_respects_custom_metric_name(monkeypatch):
    captured = {}

    def fake_samples(region, namespace, metric_name, dimensions, window_seconds, stat="Average"):
        captured["metric_name"] = metric_name
        return np.array([])

    monkeypatch.setattr(cwc, "fetch_metric_samples", fake_samples)

    cwc.fetch_saturation_samples("us-east-1", "checkout", "canary", window_seconds=120, metric_name="MemoryUtilization")

    assert captured["metric_name"] == "MemoryUtilization"


def test_target_group_name_is_truncated_to_32_chars(monkeypatch):
    """AWS target group names cannot exceed 32 characters — must match
    set_traffic_weights' own [:32] truncation convention exactly, or this
    module would query a target group name that was never actually created."""
    seen_names = []

    def fake_resolve_tg(region, name):
        seen_names.append(name)
        return f"targetgroup/{name}/1"

    monkeypatch.setattr(cwc, "resolve_load_balancer_dimension", lambda region: "app/alb/1")
    monkeypatch.setattr(cwc, "resolve_target_group_dimension", fake_resolve_tg)
    monkeypatch.setattr(cwc, "fetch_sum_over_window", lambda *a, **kw: 0)

    long_service_name = "a-genuinely-very-long-service-name-that-exceeds-limits"
    cwc.fetch_error_rate_telemetry("us-east-1", long_service_name, "canary", window_seconds=60)

    assert len(seen_names[0]) <= 32
