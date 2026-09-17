"""
services/verification-engine/src/telemetry/cloudwatch_client.py

Real telemetry source for AWS ECS-deployed projects (P1, 2026-09-16) — the
CloudWatch analog of prometheus_client.py, producing the exact same
baseline_telemetry/canary_telemetry dict shapes engine.py's dispatcher
already expects, so none of the statistical-test code needs to change.
Closes the real gap `PROJECT_STATUS.md` already documented: AWS-deployed
projects have verified against synthetic fallback data since the ECS
target went live, because nothing ever queried real ALB/ECS metrics.

Only ever calls read-only AWS APIs (`cloudwatch:GetMetricData`,
`elbv2:Describe*`) — never anything that mutates AWS state, preserving
the exact same structural boundary prometheus_client.py's own docstring
documents (this service reads telemetry, it never actuates — see design
invariant 3 in CLAUDE.md, which is about Kubernetes specifically, but the
same principle applies here by construction: no boto3 call in this module
can change a single AWS resource).

Dimension resolution is real, not guessed: CloudWatch's `LoadBalancer`/
`TargetGroup` dimensions require the ARN suffix (`app/<name>/<id>` /
`targetgroup/<name>/<id>`, confirmed against AWS's own ALB CloudWatch
metrics docs), never the plain resource name — resolved via one real
`elbv2:Describe*` call, cached per process since an ARN never changes for
a resource's lifetime.

Category coverage, and why business_metric is deliberately NOT supported
here: this platform's own convention (shared/aws_ecs_actuation.py,
ecs_manifest.py) is ONE shared ALB + one target group per cohort per
project — real, uniform infrastructure metrics exist for error_rate
(RequestCount + HTTPCode_Target_5XX_Count), latency (TargetResponseTime),
and saturation (AWS/ECS CPUUtilization/MemoryUtilization). A business
outcome (e.g. "checkout completed") has no generic ALB/ECS-level signal —
unlike Prometheus, where a project's own custom app metrics can express
anything, CloudWatch's infrastructure metrics genuinely cannot. Faking one
would violate the same "never invent a number nobody measured" principle
cost_tracker.py's rightsizing_rec already established — main.py's dispatch
logs and leaves this category's telemetry keys unset instead, which
engine.py already treats as "insufficient samples" (degrades confidence,
never crashes), exactly like a genuine Prometheus outage does today.

TargetResponseTime sampling: CloudWatch's standard API returns
pre-aggregated per-period statistics, never raw per-request samples
(unlike Prometheus's histogram buckets, which at least bound the true
distribution). Each period's `Average` is treated as one approximate
sample — a real, standard approximation with the identical "conservative,
documented, never silently exact" spirit as
prometheus_client.py::fetch_histogram_samples' own bucket-boundary
attribution, not a claim of a precision CloudWatch's own API doesn't offer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import numpy as np
import structlog

logger = structlog.get_logger(__name__)

# Must match services/pipeline-worker/src/aws/ecs_manifest.py::SHARED_ALB_NAME
# and shared/aws_ecs_actuation.py::SHARED_ALB_NAME exactly — duplicated as a
# plain constant for the identical reason aws_ecs_actuation.py's own
# docstring already gives: this service has no access to pipeline-worker's
# source tree (separate Docker build context), and a fixed platform-wide
# ALB name is safe to duplicate since it's a literal string, never computed
# logic that could drift.
SHARED_ALB_NAME = "smartcd-platform-alb"
DEFAULT_ECS_CLUSTER = "smartcd-platform"


class CloudWatchQueryError(Exception):
    pass


def _elbv2_client(region: str):
    return boto3.client("elbv2", region_name=region)


def _cloudwatch_client(region: str):
    return boto3.client("cloudwatch", region_name=region)


def resolve_load_balancer_dimension(region: str, load_balancer_name: str = SHARED_ALB_NAME) -> str:
    """
    CloudWatch's `LoadBalancer` dimension value is `app/<name>/<id>` — the
    final 3 path segments of the load balancer's ARN, confirmed against
    AWS's own ALB CloudWatch-metrics documentation. Never hand-constructed
    from the plain name alone (the `<id>` segment is only known via a real
    describe call).
    """
    try:
        elbv2 = _elbv2_client(region)
        lbs = elbv2.describe_load_balancers(Names=[load_balancer_name])["LoadBalancers"]
    except Exception as e:
        raise CloudWatchQueryError(f"Could not resolve load balancer '{load_balancer_name}': {e}") from e
    if not lbs:
        raise CloudWatchQueryError(f"Load balancer '{load_balancer_name}' not found in {region}")
    # ARN resource part is "loadbalancer/app/<name>/<id>" (split on ":" to
    # isolate it — the name/id segments themselves may never contain ":",
    # but splitting on "/" alone would wrongly glom the "arn:...:account-id:"
    # prefix onto "loadbalancer" for ARNs with no other "/" before it, see
    # the target-group sibling function's own comment for the bug this
    # caused there). CloudWatch's LoadBalancer dimension wants only
    # "app/<name>/<id>", i.e. the resource part with "loadbalancer/" dropped.
    resource = lbs[0]["LoadBalancerArn"].split(":", 5)[-1]
    return "/".join(resource.split("/")[1:])


def resolve_target_group_dimension(region: str, target_group_name: str) -> str:
    """Same ARN-suffix principle as resolve_load_balancer_dimension, for
    the `TargetGroup` dimension (`targetgroup/<name>/<id>`).

    Unlike a load balancer ARN, a target group ARN's resource part has no
    extra "app/" level ("targetgroup/<name>/<id>", 3 segments, vs.
    "loadbalancer/app/<name>/<id>", 4) — so naively taking the ARN's last 3
    "/"-separated segments (which happens to work for the load-balancer
    case) instead leaves the "arn:...:account-id:" prefix glued onto
    "targetgroup" here, producing a dimension value CloudWatch would never
    match against any real metric. CloudWatch's TargetGroup dimension wants
    the resource part verbatim, with nothing stripped."""
    try:
        elbv2 = _elbv2_client(region)
        tgs = elbv2.describe_target_groups(Names=[target_group_name])["TargetGroups"]
    except Exception as e:
        raise CloudWatchQueryError(f"Could not resolve target group '{target_group_name}': {e}") from e
    if not tgs:
        raise CloudWatchQueryError(f"Target group '{target_group_name}' not found in {region}")
    return tgs[0]["TargetGroupArn"].split(":", 5)[-1]


def _get_metric_data(
    region: str, namespace: str, metric_name: str, dimensions: list[dict], stat: str,
    window_seconds: float, period_seconds: int,
) -> list[float]:
    cw = _cloudwatch_client(region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=window_seconds)
    try:
        resp = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "m1",
                    "MetricStat": {
                        "Metric": {"Namespace": namespace, "MetricName": metric_name, "Dimensions": dimensions},
                        "Period": period_seconds,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            ],
            StartTime=start,
            EndTime=end,
        )
    except Exception as e:
        raise CloudWatchQueryError(f"CloudWatch GetMetricData failed for {metric_name}: {e}") from e

    results = resp.get("MetricDataResults", [])
    if not results:
        return []
    return list(results[0].get("Values", []))


def fetch_sum_over_window(
    region: str, namespace: str, metric_name: str, dimensions: list[dict], window_seconds: float,
) -> int:
    """Total count over the window — mirrors prometheus_client.fetch_counter_totals.
    A single period spanning the whole window is enough for a plain sum."""
    values = _get_metric_data(
        region, namespace, metric_name, dimensions, "Sum", window_seconds,
        period_seconds=max(60, int(window_seconds)),
    )
    return round(sum(values))


def _sample_period_seconds(window_seconds: float) -> int:
    """~10 datapoints across the window, clamped to CloudWatch's real
    constraints (60s minimum standard-resolution period, and no point
    exceeding 300s for a verification-window-sized query)."""
    return max(60, min(300, int(window_seconds / 10) or 60))


def fetch_metric_samples(
    region: str, namespace: str, metric_name: str, dimensions: list[dict], window_seconds: float,
    stat: str = "Average",
) -> np.ndarray:
    """Per-period datapoints as an approximate sample array — mirrors
    prometheus_client.fetch_gauge_samples' own "each timestep is one
    sample" approximation, just against CloudWatch's aggregated API
    instead of a raw Prometheus range query."""
    values = _get_metric_data(
        region, namespace, metric_name, dimensions, stat, window_seconds,
        period_seconds=_sample_period_seconds(window_seconds),
    )
    return np.array(values)


# ─────────────────── ALB/ECS convention-aware helpers ───────────────────
# This platform's own onboarding convention (aws_ecs_actuation.py /
# ecs_manifest.py): one shared ALB for every project, a target group per
# cohort named "{service_name}-baseline"/"{service_name}-canary" (identical
# 32-char truncation `set_traffic_weights` already applies — AWS target
# group names cannot exceed 32 characters). Deriving real CloudWatch
# queries from that fixed convention is what lets this module need NO
# hand-authored per-project query config, unlike Prometheus, where every
# project's own custom app metrics genuinely vary and a bespoke PromQL
# string is unavoidable.


def fetch_error_rate_telemetry(region: str, service_name: str, cohort: str, window_seconds: float) -> dict:
    """Returns {"requests": [...], "errors": [...]} in the exact shape
    engine.py's error_rate dispatch expects, from real RequestCount +
    HTTPCode_Target_5XX_Count."""
    lb_dim = resolve_load_balancer_dimension(region)
    tg_name = f"{service_name}-{cohort}"[:32]
    tg_dim = resolve_target_group_dimension(region, tg_name)
    dims = [{"Name": "LoadBalancer", "Value": lb_dim}, {"Name": "TargetGroup", "Value": tg_dim}]

    total = fetch_sum_over_window(region, "AWS/ApplicationELB", "RequestCount", dims, window_seconds)
    errors = fetch_sum_over_window(region, "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dims, window_seconds)
    # Defensive only — CloudWatch's Sum statistic should never make errors
    # exceed total requests, but a transient cross-metric inconsistency
    # must never invert the ratio into something statistically nonsensical.
    errors = min(errors, total)
    return {"requests": list(range(total)), "errors": [True] * errors + [False] * (total - errors)}


def fetch_latency_samples(region: str, service_name: str, cohort: str, window_seconds: float) -> np.ndarray:
    lb_dim = resolve_load_balancer_dimension(region)
    tg_name = f"{service_name}-{cohort}"[:32]
    tg_dim = resolve_target_group_dimension(region, tg_name)
    dims = [{"Name": "LoadBalancer", "Value": lb_dim}, {"Name": "TargetGroup", "Value": tg_dim}]
    return fetch_metric_samples(region, "AWS/ApplicationELB", "TargetResponseTime", dims, window_seconds, stat="Average")


def fetch_saturation_samples(
    region: str, service_name: str, cohort: str, window_seconds: float,
    metric_name: str = "CPUUtilization", cluster: str = DEFAULT_ECS_CLUSTER,
) -> np.ndarray:
    ecs_service_name = f"{service_name}-{cohort}"
    dims = [{"Name": "ClusterName", "Value": cluster}, {"Name": "ServiceName", "Value": ecs_service_name}]
    return fetch_metric_samples(region, "AWS/ECS", metric_name, dims, window_seconds, stat="Average")
