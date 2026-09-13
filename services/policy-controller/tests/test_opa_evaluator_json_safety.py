"""
services/policy-controller/tests/test_opa_evaluator_json_safety.py

Real bug found live: a verdict's evidence can legitimately contain a
non-finite float (Fisher's exact test's odds-ratio `statistic` is
mathematically infinite whenever a contingency-table cell is exactly 0 —
a real, valid statistical result). Python's `json.dumps` emits the literal
`Infinity`/`NaN` tokens for these, which are valid Python but NOT valid
JSON — OPA's strict Go decoder rejected the whole request with a 400,
silently blocking that verdict from ever reaching OPA (and was misreported
upstream as "OPA unreachable", triggering the fail-open rollback override
for a plain client-side encoding bug, not a real OPA outage).
"""
import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.opa_evaluator import _json_safe


def test_sanitizes_infinity_and_nan_recursively():
    payload = {
        "verification_verdict": {
            "status": "HEALTHY",
            "evidence": {
                "checkout_success_rate": {
                    "statistic": float("inf"),
                    "p_value": 0.25,
                    "contingency_table": [[611, 0], [608, 2]],
                },
                "nested": {"deep": [float("nan"), 1.0, float("-inf")]},
            },
        },
        "target_stage": "step_promote",
    }

    sanitized = _json_safe(payload)

    assert sanitized["verification_verdict"]["evidence"]["checkout_success_rate"]["statistic"] is None
    assert sanitized["verification_verdict"]["evidence"]["checkout_success_rate"]["p_value"] == 0.25
    assert sanitized["verification_verdict"]["evidence"]["nested"]["deep"] == [None, 1.0, None]
    # Untouched structure/values elsewhere
    assert sanitized["target_stage"] == "step_promote"
    assert sanitized["verification_verdict"]["evidence"]["checkout_success_rate"]["contingency_table"] == [
        [611, 0], [608, 2]
    ]


def test_sanitized_payload_is_actually_valid_json():
    import json

    payload = {"a": float("inf"), "b": [float("nan"), {"c": float("-inf")}]}
    # This is the real assertion: json.dumps on the RAW payload produces
    # non-standard tokens that OPA's Go decoder rejects; on the sanitized
    # payload it must round-trip through strict JSON without raising.
    sanitized = _json_safe(payload)
    text = json.dumps(sanitized, allow_nan=False)
    assert json.loads(text) == {"a": None, "b": [None, {"c": None}]}
