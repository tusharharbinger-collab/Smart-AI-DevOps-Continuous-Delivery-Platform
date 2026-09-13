"""
services/verification-engine/tests/test_verdict_json_safety.py

Real bug found live: a user's Verification Inspector screen 500'd with
"Error: Failed to fetch" (a raw Starlette `ValueError: Out of range float
values are not JSON compliant: inf`). Root cause: Fisher's exact test's
odds-ratio `statistic` is mathematically infinite whenever a contingency
table cell is exactly 0 (a real, valid result) — Python's `json.dumps`
happily emits the literal `Infinity` token for it, but Starlette's
`JSONResponse` (and OPA's decoder, and Postgres's JSONB column) all reject
that token outright. Fixed once, at the one place every verdict is
constructed: `ImmutableVerdict.build()`.
"""
import json

from src.verdict import ImmutableVerdict


def test_build_sanitizes_non_finite_floats_in_evidence():
    verdict = ImmutableVerdict.build(
        pipeline_run_id="run-1",
        status="HEALTHY",
        composite_score=100.0,
        confidence=0.95,
        evidence={
            "checkout_success_rate": {
                "statistic": float("inf"),
                "p_value": 0.25,
                "contingency_table": [[611, 0], [608, 2]],
            },
            "nested": {"deep": [float("nan"), 1.0, float("-inf")]},
        },
    )

    assert verdict.evidence["checkout_success_rate"]["statistic"] is None
    assert verdict.evidence["checkout_success_rate"]["p_value"] == 0.25
    assert verdict.evidence["nested"]["deep"] == [None, 1.0, None]


def test_sanitized_verdict_evidence_is_actually_valid_json():
    verdict = ImmutableVerdict.build(
        pipeline_run_id="run-2",
        status="HEALTHY",
        composite_score=100.0,
        confidence=0.95,
        evidence={"a": float("inf"), "b": [float("nan")]},
    )

    # The real assertion: this must not raise, matching Starlette's
    # JSONResponse behavior (allow_nan=False) that originally 500'd.
    text = json.dumps(verdict.evidence, allow_nan=False)
    assert json.loads(text) == {"a": None, "b": [None]}


def test_canonical_json_round_trips_without_non_standard_tokens():
    verdict = ImmutableVerdict.build(
        pipeline_run_id="run-3",
        status="FAILED",
        composite_score=20.0,
        confidence=0.5,
        evidence={"x": float("inf")},
    )
    canonical = verdict.canonical_json()
    assert "Infinity" not in canonical
    assert "NaN" not in canonical
