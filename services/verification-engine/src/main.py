"""
services/verification-engine/src/main.py

FastAPI entrypoint for the Verification Engine. Exposes /healthz + /readyz
(§13.3) and a synchronous /verify endpoint that runs the statistical
dispatcher (engine.py) and publishes the resulting signed verdict to Redis —
both on the `verdicts:{pipeline_run_id}` pub/sub channel (publisher.py, for
policy-controller's subscriber loop) and as plain keys
(`verdict:{run_id}`, `latest_verdict:{run_id}`) so a REST GET (api-gateway's
verification_router) or a poll (pipeline-worker's verification_task) can
read the latest result without having been subscribed at publish time.

Deliberately has NO Kubernetes dependency and NO kubeconfig mount — see
§8.1's structural boundary.
"""
import os
from contextlib import asynccontextmanager

import redis
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from shared.logging_config import bind_request_context, clear_request_context, configure_logging

configure_logging("verification-engine")

from src.db import VerificationEngineDB
from src.engine import VerificationEngine
from src.health_router import router as health_router
from src.publisher import publish_verdict
from src.telemetry import cloudwatch_client as cw
from src.telemetry.cloudwatch_client import CloudWatchQueryError
from src.telemetry import prometheus_client as prom
from src.telemetry.prometheus_client import PrometheusQueryError
from src.promql_validator import PromQLValidationError, validate_metric_prometheus_config

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    app.state.redis_sync = redis.from_url(REDIS_URL, decode_responses=True)

    app.state.db = VerificationEngineDB()
    await app.state.db.connect()

    logger.info("verification_engine_startup")
    yield
    await app.state.db.close()
    await app.state.redis.aclose()
    logger.info("verification_engine_shutdown")


app = FastAPI(title="Verification Engine", version="1.0.0", lifespan=lifespan)
app.include_router(health_router, tags=["health"])
Instrumentator().instrument(app).expose(app, include_in_schema=False)

_engine = VerificationEngine()

# Phase 6 (§06-observability-platform-ops.md, 6.2): verdict outcome counts —
# "doubles as product-usage insight, not just ops health" per the roadmap's
# own framing. Labeled by status so a single PromQL query answers "how many
# HEALTHY/DEGRADED/FAILED verdicts in the last hour."
VERDICT_OUTCOME_COUNTER = Counter(
    "verification_verdicts_total", "Verdicts produced by /verify, by outcome", ["status"]
)


class VerifyRequest(BaseModel):
    pipeline_run_id: str
    metrics: list[dict]
    # Either supply baseline_telemetry/canary_telemetry directly (used by the
    # adversarial/unit tests and any caller that already has raw samples),
    # or set use_prometheus=True and omit them — each entry in `metrics` may
    # then carry a `prometheus` block (see _fetch_metric_from_prometheus)
    # telling this endpoint which live PromQL series to pull real data from.
    baseline_telemetry: dict = {}
    canary_telemetry: dict = {}
    use_prometheus: bool = False
    # CloudWatch telemetry (P1, 2026-09-16) — the AWS ECS analog of
    # use_prometheus. Each entry in `metrics` may carry a `cloudwatch: {}`
    # block (see _fetch_metric_from_cloudwatch) instead of/alongside a
    # `prometheus` one; unlike Prometheus, no per-metric query string is
    # needed — this platform's fixed ALB/ECS-target-group naming
    # convention is enough to derive the real CloudWatch query.
    use_cloudwatch: bool = False
    aws_region: str | None = None
    ecs_service_name: str | None = None
    ecs_cluster: str = cw.DEFAULT_ECS_CLUSTER
    elapsed_seconds: float = 0.0
    min_eval_seconds: float = 120.0
    # Phase 6 (§06-observability-platform-ops.md, 6.1): also accepted in the
    # body as a fallback — the header is what actually gets bound to this
    # request's logs (see `verify()` below), since a caller might round-trip
    # this value without bothering to also set it as a header.
    trace_id: str | None = None
    # Phase 6 (§06-observability-platform-ops.md): required to durably
    # persist this verdict to Postgres (RLS-scoped) — see db.py. Optional
    # only so direct/adversarial callers that predate this don't 422; a
    # missing tenant_id just means this one verdict isn't persisted (logged,
    # never fatal — the live Redis-backed path still works either way).
    tenant_id: str | None = None


def _fetch_metric_from_prometheus(
    metric_cfg: dict, window_seconds: float, baseline_telemetry: dict, canary_telemetry: dict
) -> None:
    """
    Mutates baseline_telemetry/canary_telemetry in place with real,
    live-scraped data for one metric — populating the exact same keys
    engine.py's dispatcher already reads (see engine.py's `_run_sprt`,
    `_run_latency`, `_run_saturation`, `_run_business`), so none of the
    statistical-test dispatch logic needs to know or care that the data
    came from Prometheus instead of a synthetic generator or a direct
    inline payload.

    `metric_cfg["prometheus"]` queries may contain a literal `{cohort}`
    placeholder, substituted with "baseline"/"canary" to produce the pair
    of queries needed for a baseline-vs-canary comparison.
    """
    name = metric_cfg["name"]
    category = metric_cfg.get("category", "latency")
    prom_cfg = metric_cfg.get("prometheus")
    if not prom_cfg:
        logger.warning("no_prometheus_config_for_metric", metric=name)
        return

    # Phase 1 hardening: validate every user-supplied PromQL string in this
    # metric's config BEFORE scheduling any real fetch — a malformed or
    # scalar-returning query is a pipeline-config bug, not a transient
    # Prometheus outage, so it must reject the run clearly rather than
    # surface as a raw exception or get silently absorbed as "missing
    # telemetry." Deliberately NOT caught here — see /verify's handler,
    # which turns this into a 422 rather than an unhandled 500.
    validate_metric_prometheus_config(prom_cfg)

    def q(template: str, cohort: str) -> str:
        # Plain substring substitution, NOT str.format() — PromQL's own label
        # selector syntax (`{cohort="...",outcome="..."}`) uses curly braces
        # too, which .format() tries to parse as format fields and chokes on
        # ("unexpected '{' in field name"). Only the literal `{cohort}`
        # placeholder token is ever meant to be substituted here.
        return template.replace("{cohort}", cohort)

    try:
        if category == "error_rate":
            b_total = prom.fetch_counter_totals(q(prom_cfg["success_query"], "baseline"), window_seconds)
            b_total += (b_errors := prom.fetch_counter_totals(q(prom_cfg["error_query"], "baseline"), window_seconds))
            c_total = prom.fetch_counter_totals(q(prom_cfg["success_query"], "canary"), window_seconds)
            c_total += (c_errors := prom.fetch_counter_totals(q(prom_cfg["error_query"], "canary"), window_seconds))
            baseline_telemetry[f"{name}_requests"] = list(range(b_total))
            baseline_telemetry[f"{name}_errors"] = [True] * b_errors + [False] * (b_total - b_errors)
            canary_telemetry[f"{name}_requests"] = list(range(c_total))
            canary_telemetry[f"{name}_errors"] = [True] * c_errors + [False] * (c_total - c_errors)

        elif category == "latency":
            baseline_telemetry[name] = prom.fetch_histogram_samples(
                q(prom_cfg["histogram_query"], "baseline"), window_seconds
            ).tolist()
            canary_telemetry[name] = prom.fetch_histogram_samples(
                q(prom_cfg["histogram_query"], "canary"), window_seconds
            ).tolist()

        elif category == "saturation":
            baseline_telemetry[name] = prom.fetch_gauge_samples(
                q(prom_cfg["gauge_query"], "baseline"), window_seconds
            ).tolist()
            canary_telemetry[name] = prom.fetch_gauge_samples(
                q(prom_cfg["gauge_query"], "canary"), window_seconds
            ).tolist()

        elif category == "business_metric":
            b_success, b_total = prom.fetch_success_total_counts(
                q(prom_cfg["success_query"], "baseline"), q(prom_cfg["total_query"], "baseline"), window_seconds
            )
            c_success, c_total = prom.fetch_success_total_counts(
                q(prom_cfg["success_query"], "canary"), q(prom_cfg["total_query"], "canary"), window_seconds
            )
            baseline_telemetry[f"{name}_success"] = b_success
            baseline_telemetry[f"{name}_total"] = b_total
            canary_telemetry[f"{name}_success"] = c_success
            canary_telemetry[f"{name}_total"] = c_total

    except PrometheusQueryError as e:
        # Missing telemetry must degrade the verdict's confidence, never
        # crash the request — leave this metric's keys unset; engine.py's
        # per-metric dispatch already handles absent data as "insufficient
        # samples" (score 100, i.e. not held against the canary) rather than
        # raising, consistent with tests/adversarial's
        # test_missing_telemetry_degrades_confidence_not_crash.
        logger.error("prometheus_fetch_failed", metric=name, error=str(e))


def _fetch_metric_from_cloudwatch(
    metric_cfg: dict,
    window_seconds: float,
    baseline_telemetry: dict,
    canary_telemetry: dict,
    aws_region: str,
    ecs_service_name: str,
    ecs_cluster: str,
) -> None:
    """
    CloudWatch analog of _fetch_metric_from_prometheus — mutates
    baseline_telemetry/canary_telemetry in place with real ALB/ECS metrics,
    populating the exact same keys engine.py's dispatcher already reads.
    See cloudwatch_client.py's own module docstring for why business_metric
    has no real signal to fetch here and is deliberately skipped rather
    than faked.
    """
    name = metric_cfg["name"]
    category = metric_cfg.get("category", "latency")
    cw_cfg = metric_cfg.get("cloudwatch")
    # `cw_cfg` is legitimately `{}` for every metric the project-YAML generator
    # emits (unlike Prometheus, no per-metric query config is needed — see this
    # module's docstring) — checking truthiness instead of `is None` would treat
    # that real, present-but-empty config as absent and skip every real fetch.
    if cw_cfg is None:
        logger.warning("no_cloudwatch_config_for_metric", metric=name)
        return

    try:
        if category == "error_rate":
            b = cw.fetch_error_rate_telemetry(aws_region, ecs_service_name, "baseline", window_seconds)
            c = cw.fetch_error_rate_telemetry(aws_region, ecs_service_name, "canary", window_seconds)
            baseline_telemetry[f"{name}_requests"] = b["requests"]
            baseline_telemetry[f"{name}_errors"] = b["errors"]
            canary_telemetry[f"{name}_requests"] = c["requests"]
            canary_telemetry[f"{name}_errors"] = c["errors"]

        elif category == "latency":
            baseline_telemetry[name] = cw.fetch_latency_samples(
                aws_region, ecs_service_name, "baseline", window_seconds
            ).tolist()
            canary_telemetry[name] = cw.fetch_latency_samples(
                aws_region, ecs_service_name, "canary", window_seconds
            ).tolist()

        elif category == "saturation":
            cw_metric_name = cw_cfg.get("metric_name", "CPUUtilization")
            baseline_telemetry[name] = cw.fetch_saturation_samples(
                aws_region, ecs_service_name, "baseline", window_seconds, cw_metric_name, ecs_cluster
            ).tolist()
            canary_telemetry[name] = cw.fetch_saturation_samples(
                aws_region, ecs_service_name, "canary", window_seconds, cw_metric_name, ecs_cluster
            ).tolist()

        elif category == "business_metric":
            # Deliberately unset, never fabricated — see cloudwatch_client.py's
            # own module docstring for why no generic ALB/ECS-level signal
            # exists for a business outcome. engine.py's dispatch already
            # treats missing telemetry as "insufficient samples," the same
            # fail-soft path a genuine Prometheus/CloudWatch outage takes.
            logger.warning("cloudwatch_business_metric_not_supported", metric=name)

    except CloudWatchQueryError as e:
        logger.error("cloudwatch_fetch_failed", metric=name, error=str(e))


@app.post("/verify")
async def verify(body: VerifyRequest, request: Request):
    # Phase 6 (§06-observability-platform-ops.md, 6.1): prefer the header
    # (what pipeline-worker's verification_task.py actually sets) over the
    # body field, and bind it for every log line this request emits.
    trace_id = request.headers.get("X-Trace-Id") or body.trace_id
    bind_request_context(trace_id=trace_id or body.pipeline_run_id)
    try:
        baseline_telemetry = dict(body.baseline_telemetry)
        canary_telemetry = dict(body.canary_telemetry)

        if body.use_prometheus:
            window_seconds = max(body.elapsed_seconds, body.min_eval_seconds, 10.0)
            for metric_cfg in body.metrics:
                try:
                    _fetch_metric_from_prometheus(metric_cfg, window_seconds, baseline_telemetry, canary_telemetry)
                except PromQLValidationError as e:
                    raise HTTPException(
                        status_code=422, detail=f"Invalid PromQL in metric '{metric_cfg.get('name')}': {e}"
                    )
        elif body.use_cloudwatch:
            if not body.aws_region or not body.ecs_service_name:
                raise HTTPException(
                    status_code=422,
                    detail="aws_region and ecs_service_name are required when use_cloudwatch=true.",
                )
            window_seconds = max(body.elapsed_seconds, body.min_eval_seconds, 10.0)
            for metric_cfg in body.metrics:
                _fetch_metric_from_cloudwatch(
                    metric_cfg, window_seconds, baseline_telemetry, canary_telemetry,
                    body.aws_region, body.ecs_service_name, body.ecs_cluster,
                )
        elif not baseline_telemetry and not canary_telemetry:
            raise HTTPException(
                status_code=422,
                detail="Provide baseline_telemetry/canary_telemetry, or set use_prometheus=true / use_cloudwatch=true.",
            )

        verdict = _engine.run_verification(
            pipeline_run_id=body.pipeline_run_id,
            metrics=body.metrics,
            baseline_telemetry=baseline_telemetry,
            canary_telemetry=canary_telemetry,
            elapsed_seconds=body.elapsed_seconds,
            min_eval_seconds=body.min_eval_seconds,
        )

        VERDICT_OUTCOME_COUNTER.labels(status=verdict.status).inc()
        publish_verdict(app.state.redis_sync, body.pipeline_run_id, verdict, trace_id=trace_id)

        verdict_dict = verdict.canonical_json()
        app.state.redis_sync.set(f"latest_verdict:{body.pipeline_run_id}", verdict_dict, ex=86400)
        from src.verdict_signer import build_signed_payload
        import json as _json
        signed_payload = build_signed_payload(verdict)
        app.state.redis_sync.set(
            f"verdict:{body.pipeline_run_id}", _json.dumps(signed_payload), ex=86400
        )

        if body.tenant_id:
            try:
                await app.state.db.record_verdict(
                    tenant_id=body.tenant_id, verdict=verdict, hmac_signature=signed_payload["signature"]
                )
            except Exception as e:
                # Never let a durability write fail the actual verification
                # response — the live Redis-backed path above already
                # succeeded and is what the dashboard/policy-controller use.
                logger.error("verdict_persist_failed", error=str(e), pipeline_run_id=body.pipeline_run_id)
        else:
            logger.warning(
                "verdict_not_persisted_no_tenant_id",
                pipeline_run_id=body.pipeline_run_id,
                reason="caller didn't supply tenant_id — this verdict won't appear in the Deployment Report/history",
            )

        return _json.loads(verdict_dict)
    finally:
        clear_request_context()
