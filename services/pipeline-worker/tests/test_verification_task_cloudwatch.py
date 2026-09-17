"""
services/pipeline-worker/tests/test_verification_task_cloudwatch.py

Covers run_verification_task's use_cloudwatch decision — real bug found
while writing this coverage: `any(m.get("cloudwatch") for m in metrics)`
treated a real, present `cloudwatch: {}` config (exactly what the
project-YAML generator emits for every AWS ECS metric — no per-metric
query string is needed, unlike prometheus) as falsy and therefore
"unconfigured," so use_cloudwatch could never actually turn True for a
real AWS ECS project. Fixed to `"cloudwatch" in m`. This mirrors the
identical class of bug fixed in verification-engine's
_fetch_metric_from_cloudwatch (`cw_cfg is None` instead of `not cw_cfg`).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.tasks import verification_task


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"status": "HEALTHY", "pipeline_run_id": json["pipeline_run_id"]})

    monkeypatch.setattr(verification_task.requests, "post", fake_post)
    return captured


def test_ecs_project_with_real_cloudwatch_blocks_sets_use_cloudwatch_true(monkeypatch):
    captured = _capture_post(monkeypatch)

    verification_task.run_verification_task(
        "run-1",
        verification_config={"metrics": [{"name": "err", "category": "error_rate", "cloudwatch": {}}]},
        deployment_target="aws_ecs",
        aws_region="us-east-1",
        ecs_service_name="checkout",
    )

    assert captured["json"]["use_cloudwatch"] is True
    assert captured["json"]["use_prometheus"] is False
    assert captured["json"]["aws_region"] == "us-east-1"
    assert captured["json"]["ecs_service_name"] == "checkout"
    # No local synthesis must happen on the real-telemetry path.
    assert captured["json"]["baseline_telemetry"] == {}
    assert captured["json"]["canary_telemetry"] == {}


def test_kubernetes_project_never_sets_use_cloudwatch_even_with_a_cloudwatch_block(monkeypatch):
    """deployment_target is the deciding factor, not metric shape alone —
    a stray cloudwatch block on a kubernetes-target pipeline must not
    flip this on."""
    captured = _capture_post(monkeypatch)

    verification_task.run_verification_task(
        "run-2",
        verification_config={"metrics": [{"name": "err", "category": "error_rate", "cloudwatch": {}}]},
        deployment_target="kubernetes",
    )

    assert captured["json"]["use_cloudwatch"] is False


def test_ecs_project_with_no_cloudwatch_blocks_falls_back_to_synthetic(monkeypatch):
    captured = _capture_post(monkeypatch)

    verification_task.run_verification_task(
        "run-3",
        verification_config={"metrics": [{"name": "err", "category": "error_rate"}]},
        deployment_target="aws_ecs",
        aws_region="us-east-1",
        ecs_service_name="checkout",
    )

    assert captured["json"]["use_cloudwatch"] is False
    assert captured["json"]["use_prometheus"] is False
    # Synthetic fallback must have actually populated telemetry.
    assert "err_requests" in captured["json"]["baseline_telemetry"]


def test_prometheus_takes_priority_over_cloudwatch_when_both_present(monkeypatch):
    captured = _capture_post(monkeypatch)

    verification_task.run_verification_task(
        "run-4",
        verification_config={
            "metrics": [
                {
                    "name": "err",
                    "category": "error_rate",
                    "prometheus": {"success_query": "up", "error_query": "down"},
                    "cloudwatch": {},
                }
            ]
        },
        deployment_target="aws_ecs",
        aws_region="us-east-1",
        ecs_service_name="checkout",
    )

    assert captured["json"]["use_prometheus"] is True
    assert captured["json"]["use_cloudwatch"] is False
