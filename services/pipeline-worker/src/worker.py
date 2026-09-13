"""
services/pipeline-worker/src/worker.py

Pipeline Worker Orchestrator — parses pipeline manifest, builds DAG, acquires tenant locks,
and executes stages in dependency order.
Spec §4.2, §4.3.
"""
import os
import json
import uuid
import asyncio
from datetime import datetime, timezone
import redis
import structlog

from src.pipeline.manifest_loader import load_pipeline, PipelineSpec
from src.pipeline.dag_builder import build_dag, execution_order
from src.pipeline.execution_state import (
    ExecutionStateStore,
    PipelineExecutionState,
    StageStatus,
)
from src.tasks.build_task import run_build_task, run_test_task
from src.tasks.deploy_task import deploy_canary_task
from src.tasks.verification_task import run_verification_task
from src.tasks.rollout_task import run_rollout_task

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


class PipelineOrchestrator:
    def __init__(self, redis_client: redis.Redis, db=None):
        self.redis = redis_client
        self.db = db
        self.state_store = ExecutionStateStore(redis_client, db)

    def _log(self, run_id: str, message: str) -> None:
        """
        Appends one line to a durable Redis LIST (`logs:{run_id}`) — the
        "Live execution log" panel (api-gateway's logs_router.py) polls
        this list rather than subscribing to a pub/sub channel. Real bugs
        found live, in order: (1) nothing anywhere in this codebase ever
        published any log line at all, so the panel showed "Waiting for
        log output…" forever, for every run; (2) fixed with plain pub/sub
        first, but this demo pipeline (single-stage, real Prometheus
        telemetry) completes in ~100ms — measurably faster than a client
        can open an SSE connection — so pub/sub's fire-and-forget delivery
        lost every line for any run a viewer wasn't ALREADY watching before
        it started. A list is replayable: a viewer connecting after the
        fact still sees everything, not just whatever happened to arrive
        while they were subscribed.
        """
        try:
            key = f"logs:{run_id}"
            self.redis.rpush(key, message)
            self.redis.expire(key, 86400)
        except Exception as e:
            logger.warning("log_append_failed", run_id=run_id, error=str(e))

    def _register_actuation_target(self, run_id: str, spec: PipelineSpec, tenant_id: str) -> None:
        """
        Tells policy-controller WHICH service's Kubernetes objects this run's
        verdicts should actuate against. This is the only place the pipeline's
        manifest gets parsed for that information, so policy-controller (which
        has no YAML/manifest knowledge of its own) can stay a pure "verify
        signature -> ask OPA -> actuate" loop keyed only by pipeline_run_id.
        Keeping this a plain Redis key (mirroring execution_state's pattern)
        avoids adding a new DB round-trip to the verdict-handling hot path.
        """
        stage_map = {s["name"]: s for s in spec.stages}
        deploy_cfg = next(
            (s.get("config", {}) for s in stage_map.values() if s.get("type") == "deploy"), {}
        )
        canary_loop_cfg = next(
            (s.get("config", {}) for s in stage_map.values() if s.get("type") == "canary_loop"), {}
        )
        route_name = canary_loop_cfg.get("routeName") or f"{canary_loop_cfg.get('service', spec.name)}-route"
        canary_deployment_name = (
            deploy_cfg.get("deployment") or canary_loop_cfg.get("canaryDeployment") or f"{spec.name}-canary"
        )
        target = {
            "route_name": route_name,
            "namespace": spec.namespace,
            "canary_deployment_name": canary_deployment_name,
            # Real bug found live (a user noticed an empty Audit Ledger):
            # policy-controller's audit_ledger INSERT needs a tenant_id (RLS
            # NOT NULL column) but had no way to know it — this is the one
            # place that already resolves the real UUID (not the manifest's
            # human-readable slug), so it travels alongside the route/
            # namespace lookup policy-controller already does per verdict.
            "tenant_id": tenant_id,
        }
        self.redis.set(f"actuation_target:{run_id}", json.dumps(target), ex=86400)
        logger.info("actuation_target_registered", pipeline_run_id=run_id, **target)

    def start_pipeline(
        self,
        manifest_path: str,
        pipeline_run_id: str | None = None,
        pipeline_id: str | None = None,
        tenant_id: str | None = None,
        resume_from_stage: str | None = None,
        trace_id: str | None = None,
    ) -> dict:
        """
        `trace_id` (Phase 6, §06-observability-platform-ops.md): the trace
        id bound for this run's log lines is also handed to verification-
        engine as an `X-Trace-Id` header on the `/verify` call, so its own
        logs for the SAME run carry it too — this is what makes "pull every
        log line for one pipeline_run_id across all services" possible
        without each service inventing its own unrelated id.

        `pipeline_id` (Phase 5): threaded into execution_state so
        reconciler.py can re-fetch this run's manifest to resume it —
        without this the durable row would record a run/stage but nothing
        that could look up what to resume it as.

        `tenant_id` (Phase 5): the manifest's own `metadata.tenantId`
        (`spec.tenant_id` below) is a human-authored, human-readable slug
        (e.g. "acme-corp") — fine as a Redis lock-key component, but NOT a
        real `tenants.tenant_id` UUID, which is what execution_state's
        Postgres FK column actually needs. api-gateway resolves the real
        UUID from the caller's JWT/RLS context and passes it through the
        trigger command; when supplied here it overrides the manifest's
        slug for every purpose that needs the real UUID. Falls back to the
        manifest's own value only for callers that don't supply one (the
        `__main__` block below, and any standalone/manual invocation).

        `resume_from_stage` (Phase 5): when set (only reconciler.py sets
        this), execution starts at this stage instead of the DAG's first
        stage — the stage recorded as `current_stage` when a worker died is
        RE-RUN (not skipped past), since it may not have finished; every
        stage type here is already idempotent by design (build/deploy use
        create-or-patch, verify/rollout just recompute from live state).
        """
        spec: PipelineSpec = load_pipeline(manifest_path)
        run_id = pipeline_run_id or str(uuid.uuid4())
        resolved_tenant_id = tenant_id or spec.tenant_id
        service_name = spec.name

        self._register_actuation_target(run_id, spec, resolved_tenant_id)

        # 1. Tenant concurrency isolation lock (§4.3) — skipped on resume:
        # the crashed worker's own lock (TTL up to 1h) would otherwise block
        # its own pipeline from resuming; reconciler.py force-releases it
        # before calling start_pipeline, so re-acquiring here is redundant
        # and would race against that release.
        if not resume_from_stage:
            acquired = self.state_store.acquire_tenant_lock(resolved_tenant_id, service_name)
            if not acquired:
                logger.warning("tenant_concurrency_lock_held", tenant_id=resolved_tenant_id, service=service_name)
                return {
                    "status": "REJECTED",
                    "reason": f"Concurrent pipeline already running for tenant={resolved_tenant_id}, service={service_name}",
                }

        # 2. Build DAG and topological execution order
        dag = build_dag(spec.stages)
        order = execution_order(dag)
        if resume_from_stage:
            if resume_from_stage not in order:
                raise ValueError(f"Cannot resume from unknown stage '{resume_from_stage}' (order={order})")
            order = order[order.index(resume_from_stage):]
            logger.info("resuming_pipeline_from_stage", run_id=run_id, resume_from_stage=resume_from_stage, remaining_order=order)
        else:
            logger.info("pipeline_dag_constructed", run_id=run_id, execution_order=order)

        # 3. Initial execution state
        initial_state = PipelineExecutionState(
            pipeline_run_id=run_id,
            tenant_id=resolved_tenant_id,
            pipeline_id=pipeline_id,
            service_name=service_name,
            current_stage=order[0] if order else "none",
            current_traffic_weight=0,
            status=StageStatus.RUNNING,
            last_updated=datetime.now(timezone.utc).isoformat(),
            stages=order,
        )
        self.state_store.save_state_sync(initial_state)

        # 4. Execute stages synchronously / sequentially for orchestration
        stage_map = {s["name"]: s for s in spec.stages}
        results = {}

        self._log(run_id, f"Pipeline started for {service_name} — stages: {', '.join(order)}")

        try:
            for stage_name in order:
                stage_cfg = stage_map[stage_name]
                stage_type = stage_cfg.get("type")
                config = stage_cfg.get("config", {})

                logger.info("executing_stage", run_id=run_id, stage=stage_name, stage_type=stage_type)
                self._log(run_id, f"--- Stage: {stage_name} ({stage_type}) ---")
                initial_state.current_stage = stage_name
                initial_state.last_updated = datetime.now(timezone.utc).isoformat()
                self.state_store.save_state_sync(initial_state)

                if stage_type == "build":
                    dockerfile = config.get("dockerfilePath", "sample-app/v1.1.0/Dockerfile")
                    tag = config.get("imageTag", "v1.1.0").replace("{{ .TargetVersion }}", "v1.1.0")
                    # Phase 2 hardening: a build stage that declares `repoUrl`
                    # gets its own repo cloned into an isolated workspace
                    # instead of building from a fixed local path — see
                    # tasks/git_clone.py + tasks/build_task.py's run_build_task
                    # docstring. Absent for every pipeline that doesn't set
                    # it (including the seeded demo pipelines), so behavior
                    # for those is unchanged.
                    #
                    # Real bug found live (Phase 8 follow-up): this used to
                    # ignore `config["image"]` entirely and every build was
                    # hardcoded to `localhost:5001/payments:{tag}` regardless
                    # of what a project actually declared — a project's real
                    # registry name was written into the YAML and then never
                    # read, so the canary Deployment (created at onboarding
                    # with the CORRECT name) could never pull what this stage
                    # had actually just built under a DIFFERENT name.
                    image_name = config.get("image")
                    clone_token = None
                    if config.get("repoUrl"):
                        # A short-lived per-run credential the trigger
                        # endpoint (projects_router.py) copies from the
                        # caller's own connected GitHub account, so a private
                        # clone uses THEIR access, not the server-wide
                        # GITHUB_TOKEN every tenant would otherwise share.
                        # Absent (None) falls back to the env-var lookup
                        # `repoCredentialsEnvVar` already names.
                        clone_token = self.redis.get(f"clone_token:{run_id}")
                        if isinstance(clone_token, bytes):
                            clone_token = clone_token.decode()
                    repo_config = (
                        {
                            "url": config["repoUrl"],
                            "ref": config.get("repoRef", "main"),
                            "credentialsEnvVar": config.get("repoCredentialsEnvVar"),
                            "credentialsToken": clone_token,
                        }
                        if config.get("repoUrl")
                        else None
                    )

                    registry_credential = None
                    registry_credential_id = config.get("registryCredentialId")
                    if registry_credential_id:
                        raw_cred = self.redis.get(f"registry:cred:{resolved_tenant_id}:{registry_credential_id}")
                        if not raw_cred:
                            raise RuntimeError(
                                f"Build stage names registryCredentialId={registry_credential_id!r} "
                                f"but no such credential exists for this tenant."
                            )
                        registry_credential = json.loads(raw_cred)

                    self._log(run_id, f"Building {dockerfile} -> tag {tag}")
                    res = run_build_task(
                        run_id,
                        dockerfile,
                        tag,
                        repo_config=repo_config,
                        image_name=image_name,
                        registry_credential=registry_credential,
                    )
                    results[stage_name] = res
                    if clone_token:
                        self.redis.delete(f"clone_token:{run_id}")
                    self._log(run_id, f"Build finished: {res.get('status')}")

                elif stage_type == "test":
                    test_cmd = config.get("command", "pytest sample-app/v1.1.0/tests/ -v")
                    self._log(run_id, f"Running: {test_cmd}")
                    res = run_test_task(run_id, test_cmd)
                    results[stage_name] = res
                    self._log(run_id, f"Tests finished: {res.get('status')}")

                elif stage_type == "deploy":
                    deployment = config.get("deployment", "payment-service-canary")
                    self._log(run_id, f"Deploying canary: {deployment}")
                    res = deploy_canary_task(run_id, "v1.1.0", deployment)
                    results[stage_name] = res
                    self._log(run_id, f"Deploy finished: {res.get('status')}")

                elif stage_type == "canary_loop":
                    steps = config.get("steps", [])
                    rollout_res = run_rollout_task(run_id, steps, 0, self.state_store)
                    self._log(run_id, f"Traffic step: {rollout_res.get('traffic_weight', rollout_res.get('weight', 0))}% canary — running verification")
                    verdict = run_verification_task(
                        run_id, spec.verificationConfig, trace_id=trace_id, tenant_id=resolved_tenant_id
                    )
                    results[stage_name] = {"rollout": rollout_res, "verdict": verdict}
                    self._log(
                        run_id,
                        f"Verdict: {verdict.get('status')} (confidence={verdict.get('confidence')}, "
                        f"score={verdict.get('composite_score')})",
                    )

            initial_state.status = StageStatus.COMPLETED
            initial_state.last_updated = datetime.now(timezone.utc).isoformat()
            self.state_store.save_state_sync(initial_state)
            logger.info("pipeline_completed_successfully", run_id=run_id)
            self._log(run_id, "Pipeline completed successfully.")

        except Exception as e:
            logger.error("pipeline_failed", run_id=run_id, error=str(e))
            initial_state.status = StageStatus.FAILED
            initial_state.last_updated = datetime.now(timezone.utc).isoformat()
            self.state_store.save_state_sync(initial_state)
            self._log(run_id, f"Pipeline FAILED: {e}")
            raise e
        finally:
            self.state_store.release_tenant_lock(resolved_tenant_id, service_name)

        return {
            "pipeline_run_id": run_id,
            "status": "COMPLETED",
            "stages_executed": order,
            "results": results,
        }


if __name__ == "__main__":
    r = redis.from_url(REDIS_URL)
    orchestrator = PipelineOrchestrator(r)
    result = orchestrator.start_pipeline("pipelines/payments-service-policy.yaml")
    print("Pipeline result:", result)
