"""
services/verification-engine/src/promql_validator.py

Phase 1 hardening: validates user-supplied custom PromQL (the
`prometheus.{success_query,error_query,histogram_query,gauge_query,
total_query}` strings a pipeline's `verificationConfig.metrics` entries
declare — see pipelines/payments-service-policy.yaml) BEFORE it's used to
schedule verification. Until now, `telemetry/prometheus_client.py` sent
these strings straight to Prometheus's HTTP API with zero validation — a
typo'd or malicious query surfaced only as a raw exception deep inside
`_fetch_metric_from_prometheus`, or (with `PrometheusQueryError` catching it
as a soft "missing telemetry" case) silently degraded confidence instead of
being rejected as the configuration error it actually is.

Prometheus doesn't expose a separate "validate-only" endpoint, so this
validates by actually running the query through `/api/v1/query` with a
strict 5s timeout: a 400 response with `errorType: bad_data` is a genuine
PromQL syntax error; a 200 response is syntactically valid but must still
be rejected if it evaluates to a scalar/string rather than an instant
vector or range vector — the fetch functions above all expect one or more
labeled series back, not a single bare number.
"""
from __future__ import annotations

import os

import httpx
import structlog

logger = structlog.get_logger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
VALIDATION_TIMEOUT_SECONDS = 5.0

# Prometheus's own resultType vocabulary: "vector" (instant vector) and
# "matrix" (range vector) are what every fetch_* function in
# prometheus_client.py actually knows how to consume; "scalar" and "string"
# are legal PromQL results but useless here (there's no {cohort}/{le} label
# to build a series comparison from).
_USABLE_RESULT_TYPES = {"vector", "matrix"}


class PromQLValidationError(ValueError):
    """A user-supplied PromQL query is malformed or evaluates to an unusable result type."""


def validate_promql_query(query: str, prometheus_url: str | None = None) -> None:
    """
    Raises PromQLValidationError with a clear message if `query` is not
    valid PromQL, or evaluates to a scalar/string. Deliberately distinct
    from `PrometheusQueryError` (telemetry/prometheus_client.py) — that one
    means "Prometheus is temporarily unreachable" (a transient infra
    problem, correctly degrades confidence rather than failing the run);
    this one means "this pipeline's own YAML is malformed" (a configuration
    bug that should be rejected outright, not silently tolerated).
    """
    if not query or not query.strip():
        raise PromQLValidationError("PromQL query must not be empty")

    url = f"{prometheus_url or PROMETHEUS_URL}/api/v1/query"
    try:
        resp = httpx.get(url, params={"query": query}, timeout=VALIDATION_TIMEOUT_SECONDS)
    except httpx.TimeoutException as e:
        raise PromQLValidationError(
            f"PromQL validation timed out after {VALIDATION_TIMEOUT_SECONDS}s for query: {query!r}"
        ) from e
    except httpx.HTTPError as e:
        raise PromQLValidationError(f"Could not reach Prometheus to validate query {query!r}: {e}") from e

    try:
        body = resp.json()
    except ValueError as e:
        raise PromQLValidationError(f"Prometheus returned a non-JSON response validating {query!r}: {e}") from e

    if resp.status_code != 200 or body.get("status") != "success":
        error_type = body.get("errorType", "unknown")
        error_detail = body.get("error", "no further detail")
        raise PromQLValidationError(f"Invalid PromQL query {query!r}: [{error_type}] {error_detail}")

    result_type = body.get("data", {}).get("resultType")
    if result_type not in _USABLE_RESULT_TYPES:
        raise PromQLValidationError(
            f"PromQL query {query!r} evaluates to a '{result_type}' result — only an instant "
            f"vector ('vector') or range vector ('matrix') is usable here, not scalar/string. "
            f"Wrap a bare aggregation (e.g. sum(...)) around a series-producing selector instead."
        )


def validate_metric_prometheus_config(prom_cfg: dict) -> None:
    """
    Validates every query string a metric's `prometheus` block declares.
    Called once per metric before scheduling verification — see
    main.py's `_fetch_metric_from_prometheus`, which calls this before ever
    substituting {cohort} and executing the real baseline/canary fetch.
    """
    query_fields = ("success_query", "error_query", "histogram_query", "gauge_query", "total_query")
    for field in query_fields:
        query = prom_cfg.get(field)
        if query:
            try:
                validate_promql_query(query)
            except PromQLValidationError as e:
                raise PromQLValidationError(f"{field}: {e}") from e
