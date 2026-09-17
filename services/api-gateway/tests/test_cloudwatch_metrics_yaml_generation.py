"""
services/api-gateway/tests/test_cloudwatch_metrics_yaml_generation.py

CloudWatch telemetry (P1, 2026-09-16) — real gap this closes: an AWS ECS
project's generated pipeline YAML had zero `cloudwatch` query blocks on
any metric, so every AWS-deployed project verified against synthetic
fallback telemetry forever, exactly like the identical Prometheus gap
already documented for Kubernetes projects. Mirrors
test_deploy_policy_yaml_generation.py's real-render-then-parse approach —
the generated YAML must be genuinely valid, not just string-match "looks
right".
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml

from src.routers.projects_router import CreateProjectRequest, generate_project_pipeline_yaml


def _render(body: CreateProjectRequest) -> dict:
    rendered = generate_project_pipeline_yaml(body, "tenant-1", "ns-1", "/api/v1/test-svc")
    return yaml.safe_load(rendered)


def _metrics(body: CreateProjectRequest) -> list[dict]:
    return _render(body)["spec"]["verificationConfig"]["metrics"]


def _base_request(**overrides) -> CreateProjectRequest:
    defaults = dict(name="test-svc", source_type="existing_image", container_image="registry.internal/test-svc")
    defaults.update(overrides)
    return CreateProjectRequest(**defaults)


def test_kubernetes_project_gets_no_cloudwatch_block():
    metrics = _metrics(_base_request(deploy_target="kubernetes"))
    assert len(metrics) == 2
    for m in metrics:
        assert "cloudwatch" not in m


def test_aws_ecs_project_gets_a_cloudwatch_block_on_every_metric():
    metrics = _metrics(_base_request(deploy_target="aws_ecs", aws_region="us-east-1"))
    assert len(metrics) == 2
    for m in metrics:
        assert m["cloudwatch"] == {}


def test_aws_ecs_project_error_rate_metric_shape_unchanged_otherwise():
    metrics = _metrics(_base_request(deploy_target="aws_ecs", aws_region="us-east-1"))
    error_rate = next(m for m in metrics if m["category"] == "error_rate")
    assert error_rate["tier"] == "critical"
    assert error_rate["alpha"] == 0.01
    assert error_rate["p0"] == 0.005
    assert error_rate["p1"] == 0.02


def test_aws_ecs_project_latency_metric_shape_unchanged_otherwise():
    metrics = _metrics(_base_request(deploy_target="aws_ecs", aws_region="us-east-1"))
    latency = next(m for m in metrics if m["category"] == "latency")
    assert latency["tier"] == "important"
    assert latency["alpha"] == 0.05
    assert latency["weight"] == 2.5


def test_canary_loop_stage_still_carries_deployment_target_alongside_cloudwatch():
    """The cloudwatch block is new; the deploymentTarget/awsRegion fields on
    canary_loop that verification_task.py reads to decide use_cloudwatch
    must still be exactly where they already were."""
    rendered = _render(_base_request(deploy_target="aws_ecs", aws_region="us-west-2"))
    canary_loop_cfg = next(s for s in rendered["spec"]["stages"] if s["type"] == "canary_loop")["config"]
    assert canary_loop_cfg["deploymentTarget"] == "aws_ecs"
    assert canary_loop_cfg["awsRegion"] == "us-west-2"
