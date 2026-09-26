"""
services/policy-controller/tests/test_ecs_rightsizing.py

Backlog #6 - connecting real CloudWatch utilization to the (already-existing) right-sizing formula for
ECS services. CloudWatch is faked; this covers our conversion from percent-of-reserved to absolute usage,
the evidence threshold, snapping to REAL Fargate sizes, and the honesty rules (no data -> no number).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import asyncio
from unittest.mock import MagicMock

import pytest

from src import cost_tracker_ecs
from src.cost_tracker_ecs import _percentile, compute_ecs_rightsizing, snap_to_fargate_size

FOOTPRINT_1VCPU_2GB = {"desired_count": 2, "cpu_vcpu": 1.0, "mem_gib": 2.0}


class FakeCloudWatch:
    def __init__(self, cpu, mem, raises=None):
        self.series = {"CPUUtilization": cpu, "MemoryUtilization": mem}
        self.raises = raises
        self.requests = []

    def get_metric_statistics(self, **kw):
        self.requests.append(kw)
        if self.raises:
            raise self.raises
        return {"Datapoints": [{"Average": v} for v in self.series[kw["MetricName"]]]}


def rec_for(cpu, mem, footprint=FOOTPRINT_1VCPU_2GB, **kw):
    return compute_ecs_rightsizing("us-east-1", "smartcd-platform", "orders-baseline", footprint,
                                   cloudwatch=FakeCloudWatch(cpu, mem), **kw)


def test_percentile_is_nearest_rank_and_always_an_observed_value():
    data = list(range(1, 101))  # 1..100
    assert _percentile(data, 95) == 95
    assert _percentile([7], 95) == 7
    assert _percentile([3, 1, 2], 100) == 3


def test_a_mostly_idle_service_is_flagged_over_provisioned_with_a_real_smaller_size():
    rec = rec_for([5.0] * 30, [12.0] * 30)
    assert rec["is_overprovisioned"] is True
    # 5% of 1 vCPU = 0.05 vCPU; 12% of 2 GiB = 0.24 GiB (p95 of a flat series is the value itself).
    assert rec["efficiency_cpu"] == pytest.approx(0.05) and rec["efficiency_mem"] == pytest.approx(0.12)
    # ...and the recommendation is a DEPLOYABLE Fargate size, not "0.0625 vCPU".
    assert rec["recommended_fargate_size"] == {"cpu": 256, "memory": 512, "cpu_vcpu": 0.25, "mem_gib": 0.5}
    assert rec["requires_approval_role"] == "platform-admin"  # always a recommendation, never auto-applied


def test_a_busy_service_is_reported_right_sized():
    rec = rec_for([70.0] * 30, [65.0] * 30)
    assert rec["is_overprovisioned"] is False
    assert rec["efficiency_cpu"] == pytest.approx(0.7)


def test_the_p95_not_the_average_drives_the_recommendation():
    # Mostly quiet with a real spike: the average (~12%) would look wasteful, the p95 (90%) says it is needed.
    cpu = [5.0] * 90 + [90.0] * 10
    rec = rec_for(cpu, [40.0] * 100)
    assert rec["observed_cpu_p95_percent"] == 90.0
    assert rec["is_overprovisioned"] is False


def test_evidence_is_recorded_so_the_ui_can_show_how_sure_this_is():
    rec = rec_for([5.0] * 12, [10.0] * 15)
    assert rec["source"] == "cloudwatch" and rec["measured_service"] == "orders-baseline"
    assert rec["sample_count"] == 12  # the smaller of the two series
    assert rec["window_seconds"] == 3600 and "baseline" in rec["caveat"]
    assert rec["requested_cpu_vcpu"] == 1.0 and rec["requested_mem_gib"] == 2.0


def test_too_few_datapoints_means_no_recommendation_not_a_guess():
    assert rec_for([5.0] * 9, [5.0] * 9) is None  # default minimum is 10 one-minute samples
    assert rec_for([5.0] * 30, [5.0] * 3) is None  # BOTH series need enough evidence
    assert rec_for([], []) is None


def test_the_evidence_threshold_is_configurable():
    assert rec_for([5.0] * 3, [5.0] * 3, min_samples=3) is not None


def test_cloudwatch_failure_is_fail_soft_none_never_raises():
    cw = FakeCloudWatch([], [], raises=RuntimeError("AccessDenied"))
    assert compute_ecs_rightsizing("us-east-1", "c", "s", FOOTPRINT_1VCPU_2GB, cloudwatch=cw) is None


def test_a_zero_sized_footprint_is_not_divided_by():
    assert rec_for([5.0] * 30, [5.0] * 30, footprint={"desired_count": 1, "cpu_vcpu": 0.0, "mem_gib": 0.0}) is None


def test_queries_the_ecs_service_metrics_for_the_measured_service_only():
    cw = FakeCloudWatch([5.0] * 30, [5.0] * 30)
    compute_ecs_rightsizing("us-east-1", "smartcd-platform", "orders-baseline", FOOTPRINT_1VCPU_2GB, cloudwatch=cw)
    assert {r["MetricName"] for r in cw.requests} == {"CPUUtilization", "MemoryUtilization"}
    for r in cw.requests:
        assert r["Namespace"] == "AWS/ECS" and r["Period"] == 60
        assert {"Name": "ServiceName", "Value": "orders-baseline"} in r["Dimensions"]
        assert {"Name": "ClusterName", "Value": "smartcd-platform"} in r["Dimensions"]


def test_already_the_smallest_size_is_never_called_over_provisioned():
    # 256 CPU / 512 MiB is Fargate's floor: there is nothing smaller to recommend, however idle it is.
    rec = rec_for([1.0] * 30, [1.0] * 30, footprint={"desired_count": 1, "cpu_vcpu": 0.25, "mem_gib": 0.5})
    assert rec["is_overprovisioned"] is False
    assert rec["recommended_fargate_size"]["cpu"] == 256 and rec["recommended_fargate_size"]["memory"] == 512


# ─────────────── snapping to real Fargate sizes ───────────────


@pytest.mark.parametrize("cpu,mem,expected", [
    (0.05, 0.064, (256, 512)),      # the raw formula's floor -> Fargate's smallest task
    (0.25, 0.5, (256, 512)),
    (0.3, 0.5, (512, 1024)),        # just over 0.25 vCPU needs the next CPU tier (and its 1 GiB memory floor)
    (0.25, 1.5, (256, 2048)),       # memory rounds up to an allowed value
    (1.0, 2.0, (1024, 2048)),
    (0.5, 4.5, (1024, 5120)),       # 0.5 vCPU tops out at 4 GiB, so a bigger memory need forces 1 vCPU
    (3.0, 6.0, (4096, 8192)),
])
def test_snap_picks_the_cheapest_real_fargate_size_that_covers_the_need(cpu, mem, expected):
    snapped = snap_to_fargate_size(cpu, mem)
    assert (snapped["cpu"], snapped["memory"]) == expected


def test_snap_returns_none_beyond_the_supported_range():
    assert snap_to_fargate_size(8.0, 16.0) is None  # > 4 vCPU: not covered rather than guessed


def test_every_snapped_size_is_a_combination_ecs_actually_accepts():
    valid = cost_tracker_ecs._FARGATE_SIZES
    for cpu in (0.05, 0.3, 0.6, 1.2, 2.5, 3.9):
        for mem in (0.064, 0.7, 3.0, 9.0, 20.0):
            snapped = snap_to_fargate_size(cpu, mem)
            if snapped:
                assert snapped["memory"] in valid[snapped["cpu"]]


# ─────────────── wired into compute_and_record_cost_ecs ───────────────


class FakeDB:
    def __init__(self):
        self.recorded = []

    async def record_cost_analysis(self, **kw):
        self.recorded.append(kw)


def _wire(monkeypatch, rec):
    footprint = {"desired_count": 1, "cpu_vcpu": 0.25, "mem_gib": 0.5}
    monkeypatch.setattr(cost_tracker_ecs, "_read_ecs_service_footprint", lambda ecs, cluster, name: dict(footprint))
    monkeypatch.setattr(cost_tracker_ecs.boto3, "client", lambda *a, **k: MagicMock())
    seen = {}

    def fake_rs(region, cluster, service, fp, **kw):
        seen.update(region=region, cluster=cluster, service=service)
        return rec

    monkeypatch.setattr(cost_tracker_ecs, "compute_ecs_rightsizing", fake_rs)
    return seen


def test_the_recommendation_is_persisted_and_measured_on_the_baseline_service(monkeypatch):
    rec = {"is_overprovisioned": True, "source": "cloudwatch"}
    seen = _wire(monkeypatch, rec)
    db = FakeDB()

    asyncio.run(cost_tracker_ecs.compute_and_record_cost_ecs(
        "run-1", "tenant-1", "orders-baseline", "orders-canary", "us-east-1", db=db))

    assert db.recorded[0]["rightsizing_rec"] == rec
    assert seen["service"] == "orders-baseline"  # a 10%-traffic canary would always look over-provisioned


def test_no_recommendation_is_recorded_as_null_and_the_cost_delta_still_is(monkeypatch):
    _wire(monkeypatch, None)
    db = FakeDB()

    result = asyncio.run(cost_tracker_ecs.compute_and_record_cost_ecs(
        "run-1", "tenant-1", "orders-baseline", "orders-canary", "us-east-1", db=db))

    assert result is not None and db.recorded[0]["rightsizing_rec"] is None
    assert "baseline_cost" in db.recorded[0]
