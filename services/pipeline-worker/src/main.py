"""
services/pipeline-worker/src/main.py

FastAPI entrypoint for the Pipeline Worker. Exposes /healthz + /readyz
(§13.3) and, on startup, launches a background consumer reading from the
`stream:pipeline:start` Redis Stream that api-gateway's pipeline_router
publishes to (§4.2) — closing the loop between "Run" button → API Gateway →
this worker's PipelineOrchestrator, which builds the DAG and executes
stages against the Kind cluster.

Phase 5 (§05-reliability-scale.md): this used to be a bare `pubsub.
subscribe("pipeline:start")` — which delivers every message to EVERY
subscriber, so 2 replicas of this service would each independently process
the same triggered run. Now uses a Redis Streams consumer group
(shared/redis_streams.py): exactly one consumer in the group gets each
entry, an explicit XACK confirms successful processing, and a periodic
sweep reclaims entries left pending by a consumer that died mid-processing
(retried up to MAX_DELIVERY_ATTEMPTS times, then dead-lettered — logged and
the pipeline marked FAILED rather than retried forever).

`PipelineOrchestrator.start_pipeline` is synchronous and blocking (it shells
out to docker/kubectl-equivalent calls per stage), so each triggered run is
handed to a worker thread via `asyncio.to_thread` rather than blocking the
event loop that also serves /healthz.
"""
import asyncio
import json
import os
import socket
import tempfile
from contextlib import asynccontextmanager

import redis
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, HTTPException
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator

from shared.logging_config import bind_request_context, clear_request_context, configure_logging

configure_logging("pipeline-worker")

from shared import redis_streams as streams
from src.health_router import router as health_router
from src.worker import PipelineOrchestrator
from src.db import PipelineWorkerDB
from src.pipeline.reconciler import reconcile_interrupted_pipelines
from src.k8s.manifest_generator import ServiceOnboardingSpec
from src.k8s.onboarding import onboard_service

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

STREAM_PIPELINE_START = "stream:pipeline:start"
GROUP_PIPELINE_WORKERS = "pipeline_workers"
CONSUMER_NAME = socket.gethostname()

MAX_DELIVERY_ATTEMPTS = 3
STALE_CLAIM_MIN_IDLE_MS = 30_000
RECONCILE_INTERVAL_SECONDS = 60
METRICS_UPDATE_INTERVAL_SECONDS = 10

# Phase 6 (§06-observability-platform-ops.md, 6.2): queue depth/pending —
# "how deep is the task queue right now" was explicitly called out as
# unanswerable without this. Pending growing while depth stays flat means
# consumers are falling behind or dying mid-processing (Phase 5's stale-
# pending reclaim is what recovers them); depth growing means triggers are
# arriving faster than any replica can drain them.
QUEUE_DEPTH_GAUGE = Gauge(
    "pipeline_queue_depth", "Total entries on the pipeline:start stream (processed + unprocessed)"
)
QUEUE_PENDING_GAUGE = Gauge(
    "pipeline_queue_pending", "Entries delivered to a consumer but not yet acked"
)


async def _process_pipeline_start_message(app: FastAPI, payload: dict) -> None:
    run_id = payload["pipeline_run_id"]
    pipeline_id = payload.get("pipeline_id")
    tenant_id = payload.get("tenant_id")
    # Phase 6 (§06-observability-platform-ops.md, 6.1): the trace_id
    # api-gateway's middleware generated for the triggering HTTP request —
    # binding it here means every log line this service emits while
    # processing this run (including the ones from `asyncio.to_thread`,
    # which copies the calling context into the new thread) carries it too.
    trace_id = payload.get("trace_id")
    policy_yaml = payload["policy_yaml"]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(policy_yaml)
        manifest_path = tmp.name

    bind_request_context(trace_id=trace_id or run_id, tenant_id=tenant_id)
    try:
        logger.info("pipeline_start_received", pipeline_run_id=run_id, consumer=CONSUMER_NAME)
        await asyncio.to_thread(
            app.state.orchestrator.start_pipeline,
            manifest_path,
            pipeline_run_id=run_id,
            pipeline_id=pipeline_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
        )
    finally:
        clear_request_context()


async def _consume_pipeline_start(app: FastAPI):
    await streams.ensure_group(app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS)
    logger.info("pipeline_worker_listening", stream=STREAM_PIPELINE_START, consumer=CONSUMER_NAME)

    while True:
        try:
            result = await streams.read_one(
                app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS, CONSUMER_NAME, block_ms=5000
            )
            if result is None:
                continue
            message_id, payload = result
            try:
                await _process_pipeline_start_message(app, payload)
                await streams.ack(app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS, message_id)
            except Exception as e:
                logger.error(
                    "pipeline_start_processing_failed", message_id=message_id, error=str(e), consumer=CONSUMER_NAME
                )
                # Deliberately NOT acked — stays pending for
                # _reclaim_stale_pending_loop to retry (or dead-letter).
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("pipeline_start_consumer_failed", error=str(e))
            await asyncio.sleep(1)


async def _reclaim_stale_pending_loop(app: FastAPI):
    """
    Recovers entries left pending by a consumer that died before acking
    (crashed mid-`start_pipeline`) — without this, that entry would sit in
    the group's pending-entries list forever and its pipeline would never
    be retried by anyone. Runs on the same interval as reconciliation.
    """
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            claimed = await streams.claim_stale_pending(
                app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS, CONSUMER_NAME,
                min_idle_ms=STALE_CLAIM_MIN_IDLE_MS,
            )
            for message_id, payload in claimed:
                attempts = await streams.get_delivery_count(
                    app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS, message_id
                )
                if attempts > MAX_DELIVERY_ATTEMPTS:
                    logger.error(
                        "pipeline_start_dead_lettered",
                        message_id=message_id,
                        pipeline_run_id=payload.get("pipeline_run_id"),
                        attempts=attempts,
                    )
                    await streams.ack(app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS, message_id)
                    continue
                logger.warning(
                    "pipeline_start_retrying_stale_message",
                    message_id=message_id,
                    pipeline_run_id=payload.get("pipeline_run_id"),
                    attempts=attempts,
                )
                try:
                    await _process_pipeline_start_message(app, payload)
                    await streams.ack(app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS, message_id)
                except Exception as e:
                    logger.error("pipeline_start_retry_failed", message_id=message_id, error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("reclaim_stale_pending_loop_failed", error=str(e))


async def _metrics_update_loop(app: FastAPI):
    while True:
        try:
            QUEUE_DEPTH_GAUGE.set(await streams.stream_length(app.state.redis, STREAM_PIPELINE_START))
            QUEUE_PENDING_GAUGE.set(
                await streams.pending_count(app.state.redis, STREAM_PIPELINE_START, GROUP_PIPELINE_WORKERS)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("metrics_update_loop_failed", error=str(e))
        await asyncio.sleep(METRICS_UPDATE_INTERVAL_SECONDS)


async def _reconcile_loop(app: FastAPI):
    """
    Periodic sweep (not just once at startup) — a worker could crash while
    OTHER worker replicas are healthy and never restart, so relying only on
    "runs once when a worker boots" would leave that interrupted pipeline
    stuck forever if no worker happens to restart afterward.
    """
    while True:
        try:
            await reconcile_interrupted_pipelines(app.state.orchestrator.state_store, app.state.orchestrator)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("reconcile_loop_failed", error=str(e))
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    app.state.redis_sync = redis.from_url(REDIS_URL, decode_responses=True)

    app.state.db = PipelineWorkerDB()
    await app.state.db.connect()

    app.state.orchestrator = PipelineOrchestrator(app.state.redis_sync, app.state.db)

    tasks = [
        asyncio.create_task(_consume_pipeline_start(app)),
        asyncio.create_task(_reclaim_stale_pending_loop(app)),
        asyncio.create_task(_reconcile_loop(app)),
        asyncio.create_task(_metrics_update_loop(app)),
    ]
    logger.info("pipeline_worker_startup", consumer=CONSUMER_NAME)
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await app.state.db.close()
    await app.state.redis.aclose()
    logger.info("pipeline_worker_shutdown")


app = FastAPI(title="Pipeline Worker", version="1.0.0", lifespan=lifespan)
app.include_router(health_router, tags=["health"])
# Phase 6 (§06-observability-platform-ops.md, 6.2): RED metrics at /metrics;
# QUEUE_DEPTH_GAUGE/QUEUE_PENDING_GAUGE (below) are updated by the consumer
# loop and scraped the same way.
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.post("/services/onboard")
async def onboard_service_now(body: dict):
    """
    Phase 3 onboarding endpoint (§03-multi-service-onboarding.md, 3.3):
    generates Deployment/Service/HTTPRoute manifests for a new service,
    applies them to the real cluster, and returns the generated Pipeline
    YAML for the caller (api-gateway's /api/v1/services) to register via
    the existing POST /api/v1/pipelines path — unchanged from a hand-written
    one as far as every downstream trigger/rollout/verify step is concerned.
    """
    # Phase 8 follow-up ("Existing Image" wizard tab): a registry credential
    # id travels in this payload, never the credential itself — this worker
    # reads the real secret material directly from the SAME Redis instance
    # api-gateway's registry_router.py wrote it to (both share REDIS_URL),
    # so the raw dockerconfigjson never appears in an HTTP request body.
    dockerconfigjson_b64: str | None = None
    image_pull_secret_name: str | None = None
    registry_credential_id = body.get("registry_credential_id")
    if registry_credential_id:
        raw = await app.state.redis.get(
            f"registry:cred:{body['tenant_id']}:{registry_credential_id}"
        )
        if not raw:
            raise HTTPException(
                status_code=422,
                detail=f"registry_credential_id '{registry_credential_id}' not found for this tenant",
            )
        stored = json.loads(raw)
        dockerconfigjson_b64 = stored["dockerconfigjson_b64"]
        image_pull_secret_name = f"{body['service_name']}-registry-cred"

    try:
        spec = ServiceOnboardingSpec(
            service_name=body["service_name"],
            image=body["image"],
            baseline_tag=body["baseline_tag"],
            canary_tag=body["canary_tag"],
            tenant_id=body["tenant_id"],
            port=body.get("port", 8080),
            health_check_path=body.get("health_check_path", "/healthz"),
            path_prefix=body.get("path_prefix"),
            namespace=body.get("namespace", "production"),
            image_pull_secret_name=image_pull_secret_name,
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")

    try:
        result = await asyncio.to_thread(onboard_service, spec, dockerconfigjson_b64)
    except Exception as e:
        logger.error("onboarding_failed", service_name=body.get("service_name"), error=str(e))
        raise HTTPException(status_code=502, detail=f"Kubernetes onboarding failed: {e}")

    return result


@app.post("/pipelines/start")
async def start_pipeline_now(body: dict):
    """Manual/synchronous trigger — useful for local testing without Redis."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(body["policy_yaml"])
        manifest_path = tmp.name
    result = await asyncio.to_thread(
        app.state.orchestrator.start_pipeline,
        manifest_path,
        pipeline_run_id=body.get("pipeline_run_id"),
        pipeline_id=body.get("pipeline_id"),
        tenant_id=body.get("tenant_id"),
        trace_id=body.get("trace_id"),
    )
    return result
