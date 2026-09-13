"""
services/verification-engine/src/telemetry/prometheus_client.py

Real telemetry source for the verification engine (Phase 2 of
docs/roadmap/02-real-telemetry.md) — a thin PromQL query client that turns
live-scraped Prometheus data into the exact same `baseline_telemetry` /
`canary_telemetry` dict shapes `engine.py`'s dispatcher already expects, so
none of the statistical-test code needs to change. This is the module named
in the original spec that was never implemented; `pipeline-worker`'s
`_synthesize_telemetry` (fabricated `random.gauss()` samples) is now an
explicit, clearly-labeled fallback rather than the only path — see
`main.py`'s `/verify` handler for how a caller opts into this real path.

Only ever makes read-only HTTP GET requests to Prometheus's query API —
deliberately kept in verification-engine (not pipeline-worker) to preserve
the existing structural boundary: this service still has no Kubernetes
access of any kind, it only ever reads metrics.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import structlog

logger = structlog.get_logger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
_QUERY_TIMEOUT_SECONDS = 10.0


class PrometheusQueryError(Exception):
    pass


def _get(path: str, params: dict) -> dict:
    """Synchronous GET — engine.py's dispatcher is itself synchronous, and a
    single blocking HTTP call per query here is fine at this call volume."""
    try:
        resp = httpx.get(f"{PROMETHEUS_URL}{path}", params=params, timeout=_QUERY_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise PrometheusQueryError(f"Prometheus query failed: {e}") from e

    body = resp.json()
    if body.get("status") != "success":
        raise PrometheusQueryError(f"Prometheus returned non-success status: {body}")
    return body["data"]


def instant_query(promql: str, at: datetime | None = None) -> list[dict]:
    """Runs an instant PromQL query, returns the raw `result` vector."""
    params = {"query": promql}
    if at is not None:
        params["time"] = at.timestamp()
    data = _get("/api/v1/query", params)
    return data.get("result", [])


def range_query(promql: str, start: datetime, end: datetime, step_seconds: float) -> list[dict]:
    """Runs a range PromQL query, returns the raw `result` matrix."""
    params = {
        "query": promql,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step_seconds,
    }
    data = _get("/api/v1/query_range", params)
    return data.get("result", [])


def fetch_counter_totals(query: str, window_seconds: float) -> int:
    """
    Evaluates `increase(<query>[<window>])` and returns the total count over
    the window (rounded — `increase` extrapolates fractionally at the
    window's edges). `query` should already be a full instant-vector
    selector, e.g. `payments_requests_total{cohort="baseline",outcome="error"}`.
    """
    promql = f"increase({query}[{int(window_seconds)}s])"
    result = instant_query(promql)
    if not result:
        return 0
    return round(sum(float(r["value"][1]) for r in result))


def fetch_histogram_samples(bucket_query: str, window_seconds: float, max_samples: int = 500) -> np.ndarray:
    """
    Reconstructs an approximate raw-sample array from a cumulative
    histogram's bucket counts. Prometheus histograms only expose bucket
    boundary counts (`..._bucket{le="0.05"}` etc.), never literal per-request
    values — Mann-Whitney/KS need raw values, so each observation falling in
    bucket (prev_le, le] is approximated as landing exactly at that bucket's
    upper edge `le`. This is a standard, documented approximation: it's
    conservative in the direction of *overstating* latency (attributing
    every observation to the top of its bucket) rather than hiding a real
    regression by underclaiming it.

    `bucket_query` should be an instant-vector selector for the `_bucket`
    series without the `le` label pinned, e.g.
    `payments_request_duration_seconds_bucket{cohort="canary"}`.
    """
    promql = f"increase({bucket_query}[{int(window_seconds)}s])"
    result = instant_query(promql)
    if not result:
        return np.array([])

    # Each series in the result is one (le, count) pair — sort ascending by le.
    buckets: list[tuple[float, float]] = []
    for series in result:
        le_str = series["metric"].get("le")
        if le_str is None:
            continue
        le = float("inf") if le_str == "+Inf" else float(le_str)
        count = float(series["value"][1])
        buckets.append((le, count))
    buckets.sort(key=lambda b: b[0])

    samples: list[float] = []
    prev_cumulative = 0.0
    for le, cumulative_count in buckets:
        if le == float("inf"):
            continue  # the +Inf bucket's count equals the total; no new upper bound to sample at
        bucket_count = max(0.0, cumulative_count - prev_cumulative)
        samples.extend([le] * int(round(bucket_count)))
        prev_cumulative = cumulative_count
        if len(samples) >= max_samples:
            break

    return np.array(samples[:max_samples])


def fetch_gauge_samples(query: str, window_seconds: float, step_seconds: float = 5.0) -> np.ndarray:
    """Range query returning the raw time series of a gauge (e.g. saturation) over the window."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=window_seconds)
    result = range_query(query, start, end, step_seconds)
    if not result:
        return np.array([])
    values = [float(v[1]) for v in result[0].get("values", [])]
    return np.array(values)


def fetch_success_total_counts(success_query: str, total_query: str, window_seconds: float) -> tuple[int, int]:
    """For business-metric telemetry: returns (success_count, total_count) over the window."""
    success = fetch_counter_totals(success_query, window_seconds)
    total = fetch_counter_totals(total_query, window_seconds)
    return success, total
