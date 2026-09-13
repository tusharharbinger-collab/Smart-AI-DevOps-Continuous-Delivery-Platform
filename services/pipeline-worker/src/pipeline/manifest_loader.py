"""
services/pipeline-worker/src/pipeline/manifest_loader.py

Parses and validates the declarative pipeline YAML manifest.
Spec §4.2.
"""
import yaml
from pydantic import BaseModel
from typing import Literal, Any

from src.schemas import PipelineValidationError, validate_canary_steps, validate_guardrails


class StepConfig(BaseModel):
    trafficWeight: int
    minDuration: str          # "120s" — parsed to seconds
    minSampleSize: int
    requiresManualApproval: bool = False


class MetricConfig(BaseModel):
    name: str
    category: Literal["error_rate", "latency", "saturation", "business_metric"]
    tier: Literal["critical", "important", "business", "informational"]
    alpha: float = 0.05
    weight: float = 1.0
    p0: float | None = None   # only for error_rate/SPRT
    p1: float | None = None


class PipelineSpec(BaseModel):
    tenant_id: str
    name: str
    namespace: str
    stages: list[dict[str, Any]]
    gates: dict[str, Any]
    guardrails: dict[str, Any]
    verificationConfig: dict[str, Any]


def _validate_spec(spec: dict) -> None:
    """
    Raises PipelineValidationError with a clear, human-readable message on
    any malformed traffic-step sequence or guardrail value — see
    src/schemas.py's docstring for why only these two blocks (not every
    stage type) are strictly validated.
    """
    guardrails = spec.get("guardrails")
    if guardrails:
        validate_guardrails(guardrails)

    for stage in spec.get("stages", []):
        if stage.get("type") != "canary_loop":
            continue
        steps = stage.get("config", {}).get("steps", [])
        try:
            validate_canary_steps(steps)
        except PipelineValidationError as e:
            raise PipelineValidationError(f"Stage '{stage.get('name', '?')}': {e}") from e


def load_pipeline(path: str) -> PipelineSpec:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    metadata = raw.get("metadata", {})
    spec = raw.get("spec", {})

    _validate_spec(spec)

    return PipelineSpec(
        tenant_id=metadata.get("tenantId", "default"),
        name=metadata.get("name", "pipeline"),
        namespace=metadata.get("namespace", "production"),
        stages=spec.get("stages", []),
        gates=spec.get("gates", {}),
        guardrails=spec.get("guardrails", {}),
        verificationConfig=spec.get("verificationConfig", {}),
    )
