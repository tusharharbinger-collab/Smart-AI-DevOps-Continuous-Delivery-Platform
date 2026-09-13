"""
services/pipeline-worker/src/schemas.py

Strict Pydantic v2 validation for declarative pipeline YAML/JSON specs —
Phase 1 hardening: today's `manifest_loader.py` types stages/gates/
guardrails/verificationConfig as loose `dict[str, Any]` and never actually
validates them, so a malformed pipeline (out-of-order traffic steps, a
negative sample size, a confidence floor of 5.0) fails deep inside
execution — or worse, silently produces a nonsensical rollout — instead of
being rejected at load time with a clear error.

These models validate the two blocks that are safety-critical (the
canary_loop traffic steps, and guardrails), not every stage type, since
build/test/deploy configs don't carry the same "wrong value silently
misconfigures a rollout" risk.

One deliberate exception to "minDuration/minSampleSize must be positive":
the final `requiresManualApproval: true` step (see
pipelines/payments-service-policy.yaml's real 100% step) legitimately has
`minSampleSize: 0` / `minDuration: 0s` — it's gated by a human, not by
statistical significance, so there's nothing to wait for. Rejecting that
would reject the platform's own real, working seed pipeline.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

_DURATION_RE = re.compile(r"^(\d+)(s|m)$")


class PipelineValidationError(ValueError):
    """Raised with a human-readable message on any malformed pipeline spec."""


def parse_duration_seconds(duration_str: str) -> int:
    match = _DURATION_RE.match(duration_str.strip())
    if not match:
        raise PipelineValidationError(
            f"Invalid duration '{duration_str}' — expected a positive integer followed by "
            f"'s' (seconds) or 'm' (minutes), e.g. '120s' or '5m'."
        )
    value, unit = match.groups()
    seconds = int(value)
    return seconds * 60 if unit == "m" else seconds


class CanaryStepSchema(BaseModel):
    trafficWeight: int
    minDuration: str
    minSampleSize: int
    requiresManualApproval: bool = False

    @field_validator("trafficWeight")
    @classmethod
    def _weight_in_range(cls, v: int) -> int:
        if not (0 < v <= 100):
            raise PipelineValidationError(f"trafficWeight must be in (0, 100], got {v}")
        return v

    @model_validator(mode="after")
    def _duration_and_sample_size_positive_unless_manual_gate(self) -> "CanaryStepSchema":
        duration_seconds = parse_duration_seconds(self.minDuration)
        if self.requiresManualApproval:
            return self
        if duration_seconds <= 0:
            raise PipelineValidationError(
                f"minDuration must be > 0 seconds for an automated step (trafficWeight="
                f"{self.trafficWeight}), got '{self.minDuration}'. Only a step with "
                f"requiresManualApproval: true may skip a duration floor."
            )
        if self.minSampleSize <= 0:
            raise PipelineValidationError(
                f"minSampleSize must be a positive integer for an automated step (trafficWeight="
                f"{self.trafficWeight}), got {self.minSampleSize}. Only a step with "
                f"requiresManualApproval: true may skip a sample-size floor."
            )
        return self


class CanaryStepsSchema(BaseModel):
    """Validates the whole ordered list of steps together — order matters."""

    steps: list[CanaryStepSchema]

    @model_validator(mode="after")
    def _weights_strictly_increasing_and_end_at_100(self) -> "CanaryStepsSchema":
        if not self.steps:
            raise PipelineValidationError("canary_loop must declare at least one traffic step")

        weights = [s.trafficWeight for s in self.steps]
        for previous, current in zip(weights, weights[1:]):
            if current <= previous:
                raise PipelineValidationError(
                    f"Traffic steps must be strictly increasing — step weight {current} does not "
                    f"exceed the previous step's {previous}. Got sequence: {weights}"
                )
        if weights[-1] != 100:
            raise PipelineValidationError(
                f"The last traffic step must reach 100% (full rollout) — got sequence: {weights}"
            )
        return self


class GuardrailsSchema(BaseModel):
    autoRollbackOnVerdict: list[str] = Field(default_factory=list)
    requireMinimumConfidence: float
    minSampleSize: int
    maxPermittedCostDeltaPercent: float

    @field_validator("requireMinimumConfidence")
    @classmethod
    def _confidence_floor_bounded(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise PipelineValidationError(
                f"guardrails.requireMinimumConfidence must be in (0.0, 1.0], got {v}"
            )
        return v

    @field_validator("minSampleSize")
    @classmethod
    def _global_sample_floor_positive(cls, v: int) -> int:
        if v <= 0:
            raise PipelineValidationError(f"guardrails.minSampleSize must be a positive integer, got {v}")
        return v

    @field_validator("maxPermittedCostDeltaPercent")
    @classmethod
    def _cost_delta_non_negative(cls, v: float) -> float:
        if v < 0:
            raise PipelineValidationError(
                f"guardrails.maxPermittedCostDeltaPercent must be non-negative, got {v}"
            )
        return v


def validate_canary_steps(steps: list[dict]) -> None:
    """Raises PipelineValidationError with a clear message on any violation."""
    try:
        CanaryStepsSchema(steps=steps)
    except PipelineValidationError:
        raise
    except Exception as e:  # pydantic's own ValidationError (missing/wrong-typed keys)
        raise PipelineValidationError(_flatten_pydantic_error(e)) from e


def validate_guardrails(guardrails: dict) -> None:
    try:
        GuardrailsSchema(**guardrails)
    except PipelineValidationError:
        raise
    except Exception as e:
        raise PipelineValidationError(_flatten_pydantic_error(e)) from e


def _flatten_pydantic_error(e: Exception) -> str:
    """Pydantic's ValidationError is accurate but verbose; keep the message short and readable."""
    errors = getattr(e, "errors", None)
    if callable(errors):
        parts = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts) if parts else str(e)
    return str(e)
