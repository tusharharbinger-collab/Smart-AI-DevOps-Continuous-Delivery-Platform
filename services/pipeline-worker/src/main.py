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

import httpx
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
from src.tasks.verification_task import run_verification_task
from src.db import PipelineWorkerDB
from src.pipeline.reconciler import reconcile_interrupted_pipelines
from src.pipeline.manifest_loader import load_pipeline
from src.schemas import PipelineValidationError
from src.k8s.manifest_generator import ServiceOnboardingSpec
from src.k8s.onboarding import onboard_service, deprovision_service
from src.aws.ecs_manifest import EcsOnboardingSpec
from src.aws.ecs_onboarding import (
    onboard_ecs_service,
    set_traffic_weights as set_ecs_traffic_weights,
    deprovision_ecs_service,
)
from src.build_preview import run_build_preview

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

STREAM_PIPELINE_START = "stream:pipeline:start"
GROUP_PIPELINE_WORKERS = "pipeline_workers"
CONSUMER_NAME = socket.gethostname()

# Gate 1 (P0, 2026-09-16) — api-gateway's webhook receiver publishes here
# instead of triggering a rollout directly. See build_preview.py and
# projects_router.py::report_gate1_result for the full mechanism.
STREAM_GATE1_CHECK = "stream:gate1:check"
GROUP_GATE1_WORKERS = "gate1_workers"
API_GATEWAY_URL = os.environ.get("API_GATEWAY_URL", "http://api-gateway:8000")

MAX_DELIVERY_ATTEMPTS = 3
# Real bug found live (2026-09-17): 30s was calibrated against the original
# lightweight demo pipeline (skips straight to progressive_verify, no real
# clone/build/AWS calls). Once gate1's real build+test dry run (observed:
# 77s for a small repo) and real blue-green pipeline stages (ECS
# stabilization waits, up to 120s target-group health polling, live-URL
# verification retries — legitimately several minutes end to end) started
# actually running, EVERY real gate1 check and pipeline run exceeded 30s
# while still genuinely in progress — guaranteeing the stale-pending
# reclaim loop treated a live, working consumer as dead and handed the
# SAME message to a second consumer, producing two real, independent
# rollouts for one commit (confirmed live: gate1_check_id 444db19a was
# reclaimed and reprocessed at the 38s mark, mid-build, both copies later
# passed and each triggered its own pipeline_execution). 10 minutes is
# comfortably beyond any legitimate single real gate1/pipeline-stage
# duration observed so far, while still recovering a genuinely crashed
# worker in a reasonable time.
STALE_CLAIM_MIN_IDLE_MS = 600_000
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
    target_version = payload.get("target_version")
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
            target_version=target_version,
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


async def _process_gate1_check_message(app: FastAPI, payload: dict) -> None:
    """
    Gate 1 (P0, 2026-09-16): runs the SAME real build+test dry run the
    onboarding wizard's "Run build & test now" panel already uses
    (build_preview.py, reusing build_task.py's exact run_build_task/
    run_test_task — never a second implementation that could drift), then
    calls back into api-gateway with the real pass/fail so a webhook-
    triggered push either gets a genuine rollout or is blocked with a
    real reason — never both, never neither.
    """
    gate1_check_id = payload["gate1_check_id"]
    project_id = payload["project_id"]
    tenant_id = payload["tenant_id"]
    commit_sha = payload.get("commit_sha")
    commit_message = payload.get("commit_message")

    bind_request_context(trace_id=gate1_check_id, tenant_id=tenant_id)
    try:
        logger.info("gate1_check_received", gate1_check_id=gate1_check_id, project_id=project_id)

        # No per-project "is this repo private" flag persists anywhere
        # (the onboarding wizard only ever needs it ephemerally, at
        # wizard-time, from the connected user's own GitHub session — see
        # git_clone.py's _authenticated_url). A server-wide GITHUB_TOKEN,
        # when genuinely configured, safely covers both cases: a token
        # grants access, it never restricts a public clone. Passing
        # credentialsEnvVar UNCONDITIONALLY would reintroduce a documented
        # trap (CLAUDE.md): if GITHUB_TOKEN were named but actually unset,
        # _authenticated_url hard-fails, breaking every public-repo gate1
        # check. Only ever requested when the env var is confirmed
        # genuinely non-empty right here.
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        config = {
            "repo_url": payload["repo_url"],
            "ref": payload.get("ref") or "main",
            "root_directory": payload.get("root_directory"),
            "dockerfile_path": payload.get("dockerfile_path"),
            "language": payload.get("language"),
            "manifest_path": payload.get("manifest_path"),
            "start_command": payload.get("start_command"),
            "test_command": payload.get("test_command"),
            "repo_private": bool(github_token),
        }
        result = await asyncio.to_thread(run_build_preview, app.state.redis_sync, gate1_check_id, config)
        passed = result.get("status") == "succeeded"

        callback_body = {
            "tenant_id": tenant_id,
            "passed": passed,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            # AI-narrated failure explanation (P0, 2026-09-16): api-gateway
            # reads preview_logs:{gate1_check_id} (already written by
            # build_preview.py's own _log()) to ground the RCA it requests
            # from explainability-service's existing /stage-failure-rca.
            "gate1_check_id": gate1_check_id,
            # Test commands are non-blocking (see build_preview.py) — a
            # failure here never flips `passed`, but must still be visible
            # rather than silently absorbed, since it's a genuine signal
            # (wrong test command, or a real test failure the Docker build's
            # own layer didn't already catch).
            "test_warning": result.get("test_warning"),
        }
        if not passed:
            callback_body.update(
                {"stage": result.get("stage"), "error": result.get("error"), "human_side": result.get("human_side")}
            )

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_GATEWAY_URL}/api/v1/projects/internal/{project_id}/gate1-result", json=callback_body,
            )
            resp.raise_for_status()
        logger.info("gate1_check_completed", gate1_check_id=gate1_check_id, project_id=project_id, passed=passed)
    finally:
        clear_request_context()


async def _consume_gate1_check(app: FastAPI):
    await streams.ensure_group(app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS)
    logger.info("gate1_worker_listening", stream=STREAM_GATE1_CHECK, consumer=CONSUMER_NAME)

    while True:
        try:
            result = await streams.read_one(
                app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS, CONSUMER_NAME, block_ms=5000
            )
            if result is None:
                continue
            message_id, payload = result
            try:
                await _process_gate1_check_message(app, payload)
                await streams.ack(app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS, message_id)
            except Exception as e:
                logger.error(
                    "gate1_check_processing_failed", message_id=message_id, error=str(e), consumer=CONSUMER_NAME
                )
                # Deliberately NOT acked — stays pending for
                # _reclaim_stale_gate1_pending_loop to retry (or dead-letter).
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("gate1_check_consumer_failed", error=str(e))
            await asyncio.sleep(1)


async def _reclaim_stale_gate1_pending_loop(app: FastAPI):
    """Mirrors _reclaim_stale_pending_loop, for the gate1-check stream — a
    consumer that crashes mid-build-preview must not leave that push
    stuck with no retry forever."""
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            claimed = await streams.claim_stale_pending(
                app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS, CONSUMER_NAME,
                min_idle_ms=STALE_CLAIM_MIN_IDLE_MS,
            )
            for message_id, payload in claimed:
                attempts = await streams.get_delivery_count(
                    app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS, message_id
                )
                if attempts > MAX_DELIVERY_ATTEMPTS:
                    logger.error(
                        "gate1_check_dead_lettered",
                        message_id=message_id,
                        gate1_check_id=payload.get("gate1_check_id"),
                        attempts=attempts,
                    )
                    await streams.ack(app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS, message_id)
                    continue
                logger.warning(
                    "gate1_check_retrying_stale_message",
                    message_id=message_id,
                    gate1_check_id=payload.get("gate1_check_id"),
                    attempts=attempts,
                )
                try:
                    await _process_gate1_check_message(app, payload)
                    await streams.ack(app.state.redis, STREAM_GATE1_CHECK, GROUP_GATE1_WORKERS, message_id)
                except Exception as e:
                    logger.error("gate1_check_retry_failed", message_id=message_id, error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("reclaim_stale_gate1_pending_loop_failed", error=str(e))


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
        asyncio.create_task(_consume_gate1_check(app)),
        asyncio.create_task(_reclaim_stale_gate1_pending_loop(app)),
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


@app.post("/services/deprovision")
async def deprovision_service_now(body: dict):
    """
    Real gap found live (2026-09-15): DELETE /api/v1/projects/{id}
    (api-gateway) only ever removed the DB row — the real cluster objects
    `/services/onboard` created were left running forever, and a project
    recreated with the same name later merged onto the stale objects
    instead of a clean create (a real config-drift bug, caught live while
    proving the live_url feature). Best-effort, matching onboarding's own
    best-effort semantics exactly — a project whose cluster objects were
    never reachable/created in the first place has nothing to deprovision,
    which this reports as success (nothing deleted), not an error.
    """
    try:
        service_name = body["service_name"]
        namespace = body.get("namespace", "production")
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")

    try:
        result = await asyncio.to_thread(deprovision_service, namespace, service_name)
    except Exception as e:
        logger.error("deprovisioning_failed", service_name=service_name, error=str(e))
        raise HTTPException(status_code=502, detail=f"Kubernetes deprovisioning failed: {e}")

    return result


@app.post("/services/onboard-aws")
async def onboard_service_aws_now(body: dict):
    """
    Module 8 — AWS ECS Fargate as a real second deployment target, alongside
    /services/onboard's Kubernetes path. `image` must be a real ECR
    repository URI (no tag) that the build stage already pushed a real
    image to — see ecr_auth.py, proven working earlier this session.
    Applies every real AWS resource (cluster, IAM role, security groups,
    shared ALB, target groups, listener rule, task definitions, services)
    and returns a real, live-reachable `live_url` — not a simulated one.
    """
    try:
        spec = EcsOnboardingSpec(
            service_name=body["service_name"],
            image=body["image"],
            baseline_tag=body["baseline_tag"],
            canary_tag=body["canary_tag"],
            tenant_id=body["tenant_id"],
            port=body.get("port", 8080),
            health_check_path=body.get("health_check_path", "/healthz"),
            path_prefix=body.get("path_prefix"),
            region=body.get("region", "us-east-1"),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")

    try:
        result = await asyncio.to_thread(onboard_ecs_service, spec)
    except Exception as e:
        logger.error("ecs_onboarding_failed", service_name=body.get("service_name"), error=str(e))
        raise HTTPException(status_code=502, detail=f"AWS ECS onboarding failed: {e}")

    return result


@app.post("/services/onboard-aws/traffic-weights")
async def set_ecs_traffic_weights_now(body: dict):
    """Real weighted canary traffic shift on AWS — the ECS equivalent of
    the Kubernetes HTTPRoute weight patch."""
    try:
        service_name = body["service_name"]
        path_prefix = body["path_prefix"]
        baseline_weight = body["baseline_weight"]
        canary_weight = body["canary_weight"]
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")

    try:
        result = await asyncio.to_thread(
            set_ecs_traffic_weights,
            body.get("region", "us-east-1"),
            service_name,
            path_prefix,
            baseline_weight,
            canary_weight,
        )
    except Exception as e:
        logger.error("ecs_traffic_weight_update_failed", service_name=service_name, error=str(e))
        raise HTTPException(status_code=502, detail=f"AWS ECS traffic weight update failed: {e}")

    return result


@app.post("/services/deprovision-aws")
async def deprovision_service_aws_now(body: dict):
    """
    Real gap found live (2026-09-15): DELETE /api/v1/projects/{id}
    (api-gateway) only ever called the Kubernetes-side `/services/deprovision`
    — a project onboarded with `deploy_target: "aws_ecs"` had its real ECS
    services, target groups, and ALB listener rule left running (and
    billing) forever. Symmetric to `/services/deprovision`'s own best-effort
    semantics: a project whose AWS resources were never created has nothing
    to deprovision, reported as success (nothing deleted), not an error.
    """
    try:
        service_name = body["service_name"]
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")

    try:
        result = await asyncio.to_thread(
            deprovision_ecs_service,
            body.get("region", "us-east-1"),
            service_name,
            body.get("path_prefix"),
        )
    except Exception as e:
        logger.error("ecs_deprovisioning_failed", service_name=service_name, error=str(e))
        raise HTTPException(status_code=502, detail=f"AWS ECS deprovisioning failed: {e}")

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


@app.post("/pipelines/validate")
async def validate_pipeline_yaml(body: dict):
    """
    body: {"policy_yaml": str}
    The single source of truth for pipeline validity — other services (e.g.
    api-gateway's AI-authoring endpoint) call this over HTTP rather than
    duplicating or cross-importing schemas.py's safety-critical rules, so
    there's exactly one place these guardrails can drift out of sync from.
    Runs the exact same load_pipeline() a real triggered run goes through —
    not a separate, weaker check.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(body["policy_yaml"])
        manifest_path = tmp.name
    try:
        load_pipeline(manifest_path)
        return {"valid": True, "error": None}
    except PipelineValidationError as e:
        return {"valid": False, "error": str(e)}
    except Exception as e:
        return {"valid": False, "error": f"Malformed pipeline YAML: {e}"}


@app.post("/build-preview")
async def start_build_preview(body: dict):
    """
    body: repo_url, ref, root_directory, dockerfile_path (nullable),
    language/manifest_path/start_command (nullable — the synthesized-
    Dockerfile path when dockerfile_path is absent), test_command
    (nullable), repo_private (bool), credentials_token (nullable).

    Fires the build-only dry run (see build_preview.py) in a background
    thread and returns immediately with a run_id — the same "trigger
    returns fast, watch logs stream in separately" shape every other
    pipeline trigger in this service already uses, so the caller doesn't
    need a different client pattern for this one endpoint.
    """
    import uuid

    if not body.get("repo_url"):
        raise HTTPException(status_code=422, detail="repo_url is required")

    run_id = str(uuid.uuid4())
    asyncio.create_task(asyncio.to_thread(run_build_preview, app.state.redis_sync, run_id, body))
    return {"run_id": run_id, "status": "running"}


@app.get("/build-preview/{run_id}/result")
async def get_build_preview_result(run_id: str):
    import json as _json

    raw = await app.state.redis.get(f"preview_result:{run_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="No build preview found for this run_id (expired or never started)")
    return _json.loads(raw)


@app.get("/build-preview/{run_id}/logs")
async def get_build_preview_logs(run_id: str):
    lines = await app.state.redis.lrange(f"preview_logs:{run_id}", 0, -1)
    return {"lines": lines}


@app.post("/pipelines/{run_id}/reverify")
async def reverify_pipeline_step(run_id: str, body: dict):
    """
    Real automated-ramp hand-off (see worker.py's `_register_actuation_target`
    docstring and policy-controller's `rollout_scheduler.py`): after
    promoting a canary to one step's traffic weight, policy-controller waits
    out that step's own minDuration/minSampleSize gate and calls this
    endpoint to produce the NEXT step's real verdict — closing the loop
    that used to end every rollout after exactly one verification cycle,
    always at a hardcoded weight, regardless of how many steps a pipeline
    actually declared.

    Deliberately thin: this does NOT touch Kubernetes or build/test stages
    (actuation stays policy-controller's exclusive job — see invariant 3 in
    CLAUDE.md) — it only asks verification-engine to compare the canary's
    live telemetry again, at the real elapsed time this run has actually
    been going, and publishes the signed verdict the normal way. Every
    other verdict-handling step (HMAC verify, OPA gate, actuate) is
    unchanged — policy-controller's consumer picks this verdict up off
    `stream:verdicts` exactly like the first one.
    """
    tenant_id = body.get("tenant_id")
    verification_config = body.get("verification_config", {})
    elapsed_seconds = float(body.get("elapsed_seconds", 180.0))
    trace_id = body.get("trace_id")
    # CloudWatch telemetry (P1, 2026-09-16) — carried by rollout_scheduler.py's
    # reverify call so an AWS ECS project's SECOND-and-later verdicts use
    # real telemetry too, not just its first (see that module's own note).
    deployment_target = body.get("deployment_target", "kubernetes")
    aws_region = body.get("aws_region")
    ecs_service_name = body.get("ecs_service_name")

    bind_request_context(trace_id=trace_id or run_id, tenant_id=tenant_id)
    try:
        app.state.orchestrator._log(
            run_id, f"Traffic step advanced — running verification (elapsed={elapsed_seconds:.0f}s)"
        )
        verdict = await asyncio.to_thread(
            run_verification_task, run_id, verification_config, elapsed_seconds, trace_id, tenant_id,
            deployment_target, aws_region, ecs_service_name,
        )
        app.state.orchestrator._log(
            run_id,
            f"Verdict: {verdict.get('status')} (confidence={verdict.get('confidence')}, "
            f"score={verdict.get('composite_score')})",
        )
        return verdict
    finally:
        clear_request_context()
