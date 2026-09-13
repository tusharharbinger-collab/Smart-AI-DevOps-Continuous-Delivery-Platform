"""
Unit tests for pipeline manifest loader and DAG builder (§4.2).
"""
import os
import pytest
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.pipeline.manifest_loader import load_pipeline
from src.pipeline.dag_builder import build_dag, execution_order


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def test_load_manifest():
    manifest_path = os.path.join(_REPO_ROOT, "pipelines", "payments-service-policy.yaml")
    spec = load_pipeline(manifest_path)
    assert spec.tenant_id == "acme-corp"
    assert spec.name == "payments-service-rollout"
    assert len(spec.stages) == 4
    stage_names = [s["name"] for s in spec.stages]
    assert "build" in stage_names
    assert "test" in stage_names
    assert "canary_deploy" in stage_names
    assert "progressive_verify" in stage_names


def test_dag_execution_order():
    stages = [
        {"name": "build"},
        {"name": "test"},
        {"name": "canary_deploy", "dependsOn": ["build", "test"]},
        {"name": "progressive_verify", "dependsOn": ["canary_deploy"]},
    ]
    dag = build_dag(stages)
    order = execution_order(dag)
    assert order.index("build") < order.index("canary_deploy")
    assert order.index("test") < order.index("canary_deploy")
    assert order.index("canary_deploy") < order.index("progressive_verify")


def test_dag_cycle_detection():
    stages = [
        {"name": "stage_a", "dependsOn": ["stage_b"]},
        {"name": "stage_b", "dependsOn": ["stage_a"]},
    ]
    with pytest.raises(ValueError, match="cycle"):
        build_dag(stages)
