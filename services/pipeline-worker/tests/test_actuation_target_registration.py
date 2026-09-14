"""
services/pipeline-worker/tests/test_actuation_target_registration.py

Real bug found live (a genuine end-to-end rollout against a real Kind
cluster): `spec.name` is the pipeline's own metadata name, which every
generator (api-gateway's generate_project_pipeline_yaml,
manifest_generator.py) suffixes with "-rollout" — real onboarded
Deployments never carry that suffix. `route_name`'s fallback already
correctly used the canary_loop config's own `service` field (the real,
unsuffixed name) instead of bare `spec.name`; canary/baseline's fallbacks
didn't, so any pipeline that doesn't explicitly declare
`canaryDeployment`/`baselineDeployment` (every legacy/adopted one, e.g.
payments-pipeline, and — until this session's own fix landed —
baselineDeployment was never explicitly emitted by the wizard either)
resolved to "{name}-rollout-canary" / "{name}-rollout-baseline", a
Deployment that never existed, 404ing on every real Kubernetes call. This
went undetected because no test exercised _register_actuation_target with
a pipeline lacking those explicit keys, and prior live checks either used
a pipeline that DOES declare canaryDeployment, or called
handle_incoming_verdict with redis_client=None (the hardcoded DEFAULT_*
fallback, never actually exercising this derivation).
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.pipeline.manifest_loader import PipelineSpec
from src.worker import PipelineOrchestrator


class _FakeRedis:
    """Minimal in-memory stand-in for the sync redis.Redis calls
    PipelineOrchestrator makes (.set/.get/.rpush/.expire) — no real Redis
    server needed for this test."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def rpush(self, key, value):
        self.store.setdefault(key, [])
        self.store[key].append(value)

    def expire(self, key, seconds):
        pass


def _spec_with_rollout_suffixed_name(canary_loop_config: dict) -> PipelineSpec:
    """Mirrors the real shape every generator produces: metadata.name always
    carries the "-rollout" suffix, distinct from the service's real name."""
    return PipelineSpec(
        tenant_id="tenant-1",
        name="checkout-api-rollout",
        namespace="production",
        stages=[{"name": "canary_verify", "type": "canary_loop", "config": canary_loop_config}],
        gates={},
        guardrails={},
        verificationConfig={},
    )


@pytest.fixture
def orchestrator():
    return PipelineOrchestrator(_FakeRedis())


def test_baseline_and_canary_names_use_the_real_service_name_not_the_rollout_suffixed_one(orchestrator):
    """The gap found live: neither canaryDeployment nor baselineDeployment
    is explicitly declared — exactly the shape of every legacy/adopted
    pipeline (e.g. payments-pipeline) and, until this session, every
    wizard-generated one too."""
    spec = _spec_with_rollout_suffixed_name({"service": "checkout-api", "steps": []})

    orchestrator._register_actuation_target("run-1", spec, "tenant-1")

    target = json.loads(orchestrator.redis.get("actuation_target:run-1"))
    assert target["canary_deployment_name"] == "checkout-api-canary"
    assert target["baseline_deployment_name"] == "checkout-api-baseline"
    assert target["route_name"] == "checkout-api-route"
    # The bug: these must never contain the pipeline's internal suffix.
    assert "-rollout-" not in target["canary_deployment_name"]
    assert "-rollout-" not in target["baseline_deployment_name"]


def test_explicit_canary_and_baseline_deployment_names_are_still_honored(orchestrator):
    """A pipeline that DOES declare them explicitly (as api-gateway's
    generator does after this session's fix) must use those verbatim,
    never the derived fallback."""
    spec = _spec_with_rollout_suffixed_name(
        {
            "service": "checkout-api",
            "canaryDeployment": "checkout-api-canary",
            "baselineDeployment": "checkout-api-baseline",
            "steps": [],
        }
    )

    orchestrator._register_actuation_target("run-2", spec, "tenant-1")

    target = json.loads(orchestrator.redis.get("actuation_target:run-2"))
    assert target["canary_deployment_name"] == "checkout-api-canary"
    assert target["baseline_deployment_name"] == "checkout-api-baseline"


def test_falls_back_to_pipeline_name_only_when_canary_loop_names_no_service(orchestrator):
    """No `service` field at all (the most degraded legacy case) still
    shouldn't crash — falls back to spec.name itself, matching the
    pre-existing route_name fallback's own behavior."""
    spec = _spec_with_rollout_suffixed_name({"steps": []})

    orchestrator._register_actuation_target("run-3", spec, "tenant-1")

    target = json.loads(orchestrator.redis.get("actuation_target:run-3"))
    assert target["canary_deployment_name"] == "checkout-api-rollout-canary"
    assert target["baseline_deployment_name"] == "checkout-api-rollout-baseline"
