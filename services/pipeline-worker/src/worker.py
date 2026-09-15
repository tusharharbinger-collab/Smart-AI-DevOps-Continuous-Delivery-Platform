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
import requests
import structlog

from src.pipeline.manifest_loader import load_pipeline, PipelineSpec
from src.pipeline.dag_builder import build_dag, execution_order
from src.pipeline.execution_state import (
    ExecutionStateStore,
    PipelineExecutionState,
    StageStatus,
)
from src.tasks.build_task import run_build_task, run_test_task
from src.tasks.git_clone import cleanup_workspace
from src.tasks.deploy_task import (
    deploy_canary_task,
    deploy_project_canary_task,
    set_first_deployment_route_weights,
)
from src.tasks.ecr_auth import is_ecr_image, get_ecr_registry_credential
from src.tasks.verification_task import run_verification_task
from src.tasks.rollout_task import run_rollout_task
from src.schemas import parse_duration_seconds

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
EXPLAINABILITY_SERVICE_URL = os.environ.get("EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004")


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

    def _request_stage_failure_rca(self, run_id: str, failed_stage: str, error_message: str) -> None:
        """
        Real gap found comparing this platform against Harness's "Software
        Delivery Agent": explainability-service's grounded RCA only ever
        explained a verification verdict — a failed build/test/deploy stage
        just logged the raw exception with no explanation. This asks for one
        and stores it at `failure_rca:{run_id}` (api-gateway's
        GET /{project_id}/runs/{run_id}/failure-analysis reads it) — wrapped
        in its own try/except so an explainability-service outage can never
        prevent the FAILED state this is called right after from being
        recorded; the pipeline has already failed by the time this runs.
        """
        try:
            recent_logs = self.redis.lrange(f"logs:{run_id}", 0, -1) or []
            resp = requests.post(
                f"{EXPLAINABILITY_SERVICE_URL}/stage-failure-rca",
                json={
                    "run_id": run_id,
                    "failed_stage": failed_stage,
                    "error_message": error_message,
                    "recent_logs": recent_logs,
                },
                timeout=35.0,
            )
            resp.raise_for_status()
            self.redis.set(f"failure_rca:{run_id}", json.dumps(resp.json()), ex=86400)
            logger.info("stage_failure_rca_stored", run_id=run_id, failed_stage=failed_stage)
        except Exception as e:
            logger.warning("stage_failure_rca_request_failed", run_id=run_id, error=str(e))

    def _register_actuation_target(
        self, run_id: str, spec: PipelineSpec, tenant_id: str, target_version: str | None = None
    ) -> None:
        """
        Tells policy-controller WHICH service's Kubernetes objects this run's
        verdicts should actuate against. This is the only place the pipeline's
        manifest gets parsed for that information, so policy-controller (which
        has no YAML/manifest knowledge of its own) can stay a pure "verify
        signature -> ask OPA -> actuate" loop keyed only by pipeline_run_id.
        Keeping this a plain Redis key (mirroring execution_state's pattern)
        avoids adding a new DB round-trip to the verdict-handling hot path.

        Also registers `rollout_state:{run_id}` (see policy-controller's
        `rollout_scheduler.py`) — real gap found live: every promotion always
        jumped straight to a hardcoded 25% canary weight and the pipeline
        ended after exactly one verification cycle, regardless of the
        `steps` (10% -> 25% -> 50% -> 100%) a pipeline actually declares.
        Registering the real step schedule here is what lets
        policy-controller autonomously walk through it for real: real
        per-step target weight, real minSampleSize/minDuration gating (not
        a flat 150 requests / 300 seconds), and a real hand-off back to
        this service's new `/pipelines/{run_id}/reverify` endpoint to
        produce the next step's verdict — continuing until either a FAILED
        verdict rolls back, or a step flagged `requiresManualApproval`
        pauses for a human (see `alert_approval_required`).
        """
        stage_map = {s["name"]: s for s in spec.stages}
        deploy_cfg = next(
            (s.get("config", {}) for s in stage_map.values() if s.get("type") == "deploy"), {}
        )
        canary_loop_cfg = next(
            (s.get("config", {}) for s in stage_map.values() if s.get("type") == "canary_loop"), {}
        )
        # Real bug found live: `spec.name` is the pipeline's own metadata
        # name, which every generator (generate_project_pipeline_yaml,
        # manifest_generator.py) suffixes with "-rollout" — real onboarded
        # Deployments never carry that suffix. `route_name`'s fallback
        # already correctly used the canary_loop config's own `service`
        # field (the real, unsuffixed name) instead of bare `spec.name`;
        # canary/baseline's fallbacks didn't, so any pipeline that doesn't
        # explicitly declare `canaryDeployment`/`baselineDeployment` (every
        # legacy/adopted one, e.g. payments-pipeline) resolved to a
        # Deployment that never existed — "{name}-rollout-canary" /
        # "{name}-rollout-baseline" — 404ing on every real k8s call.
        # Invisible until now because every test this session either hit a
        # pipeline that DOES declare `canaryDeployment` explicitly, or
        # called with redis_client=None (the hardcoded DEFAULT_* fallback),
        # never actually exercising this derivation for real.
        service_name = canary_loop_cfg.get("service", spec.name)
        route_name = canary_loop_cfg.get("routeName") or f"{service_name}-route"
        canary_deployment_name = (
            deploy_cfg.get("deployment") or canary_loop_cfg.get("canaryDeployment") or f"{service_name}-canary"
        )
        # Mirrors canary_deployment_name's own resolution — needed by
        # policy-controller's cost_tracker.py to read the LIVE baseline
        # Deployment's replica/resource footprint for a real cost delta
        # (previously always hardcoded to 0.0; see cost_tracker.py).
        baseline_deployment_name = (
            deploy_cfg.get("baselineDeployment") or canary_loop_cfg.get("baselineDeployment") or f"{service_name}-baseline"
        )
        target = {
            "route_name": route_name,
            "namespace": spec.namespace,
            "canary_deployment_name": canary_deployment_name,
            "baseline_deployment_name": baseline_deployment_name,
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

        raw_steps = canary_loop_cfg.get("steps", [])
        if raw_steps:
            parsed_steps = [
                {
                    "trafficWeight": s["trafficWeight"],
                    "minDurationSeconds": parse_duration_seconds(s.get("minDuration", "0s")),
                    "minSampleSize": s.get("minSampleSize", 0),
                    "requiresManualApproval": s.get("requiresManualApproval", False),
                }
                for s in raw_steps
            ]
            rollout_state = {
                "steps": parsed_steps,
                "current_step_index": 0,
                "step_started_at": datetime.now(timezone.utc).isoformat(),
                "verification_config": spec.verificationConfig,
                "status": "RUNNING",
                # Carried through to graduate() once the final step
                # promotes — this is what becomes the new baseline version.
                "target_version": target_version,
                # Real gap found live: handle_incoming_verdict's
                # `pipeline_policy` parameter was NEVER supplied by its only
                # real caller (_handle_stream_payload), so every verdict —
                # for every pipeline, regardless of what its own YAML
                # actually declared — was evaluated against a generic
                # hardcoded default (confidence 0.80, minSampleSize 100,
                # cost ceiling 15%, and critically `manualApprovalRequired.
                # beforeStages: []` — meaning a step flagged
                # requiresManualApproval could never actually be gated,
                # since OPA's Rule 4 only fires when target_stage appears in
                # THAT list). Carrying the pipeline's real gates/guardrails
                # here is what lets policy-controller enforce what a
                # pipeline actually asked for instead of a generic default.
                "pipeline_policy": {"gates": spec.gates, "guardrails": spec.guardrails},
            }
            self.redis.set(f"rollout_state:{run_id}", json.dumps(rollout_state), ex=86400)

    def start_pipeline(
        self,
        manifest_path: str,
        pipeline_run_id: str | None = None,
        pipeline_id: str | None = None,
        tenant_id: str | None = None,
        resume_from_stage: str | None = None,
        trace_id: str | None = None,
        target_version: str | None = None,
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

        `target_version`: real bug found live — a pipeline's stored YAML
        bakes in a fixed `imageTag` at project-creation time (or, for the
        original demo pipeline, a literal `{{ .TargetVersion }}` placeholder
        that got replaced with the hardcoded string `"v1.1.0"` no matter
        what version was actually requested). Every rebuild therefore
        produced the exact same tag regardless of what changed in the repo.
        When the caller supplies a real per-run version (api-gateway's
        `trigger_project_rollout` already resolves and persists one to
        `pipeline_executions.target_version`), it overrides the manifest's
        static `imageTag` for the build stage of THIS run only — the stored
        pipeline YAML itself is untouched, so a run with no override keeps
        behaving exactly as before.
        """
        spec: PipelineSpec = load_pipeline(manifest_path)
        run_id = pipeline_run_id or str(uuid.uuid4())
        resolved_tenant_id = tenant_id or spec.tenant_id
        service_name = spec.name

        self._register_actuation_target(run_id, spec, resolved_tenant_id, target_version)

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
        # Set by the build stage when it clones a real repo (repoUrl
        # declared) so the test stage can run inside the SAME checkout
        # instead of pipeline-worker's own /app — see build_task.py's
        # run_build_task/run_test_task docstrings.
        repo_clone_workspace: str | None = None
        # Set by a generic (project-onboarded) deploy stage so canary_loop
        # can also patch the BASELINE Deployment to the same image on a
        # project's first-ever deployment — see the canary_loop branch's
        # first-deployment handling below.
        last_deploy_image_name: str | None = None
        last_deploy_image_tag: str | None = None

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
                    # target_version (a real per-run override) always wins;
                    # otherwise resolve the manifest's own imageTag, honoring
                    # a literal `{{ .TargetVersion }}` placeholder if present
                    # (the original demo pipeline's convention) by falling
                    # back to "v1.1.0" only when no real version was ever
                    # supplied at all — never hardcoding over a real one.
                    tag = target_version or config.get("imageTag", "v1.1.0").replace(
                        "{{ .TargetVersion }}", target_version or "v1.1.0"
                    )
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
                    elif image_name and is_ecr_image(image_name):
                        # An ECR repo needs no stored credential — a project
                        # onboarded against EKS/ECR already grants this
                        # worker's own AWS IAM identity registry access, so
                        # generate the (12h-lived) push token on the fly
                        # instead of asking a user to paste one in.
                        registry_credential = get_ecr_registry_credential(
                            os.environ.get("AWS_REGION", "us-east-1")
                        )

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
                    repo_clone_workspace = res.get("workspace")
                    if clone_token:
                        self.redis.delete(f"clone_token:{run_id}")
                    self._log(run_id, f"Build finished: {res.get('status')}")

                elif stage_type == "test":
                    test_cmd = config.get("command", "pytest sample-app/v1.1.0/tests/ -v")
                    self._log(run_id, f"Running: {test_cmd}")
                    res = run_test_task(run_id, test_cmd, cwd=repo_clone_workspace)
                    results[stage_name] = res
                    self._log(run_id, f"Tests finished: {res.get('status')}")

                elif stage_type == "deploy":
                    deployment = config.get("deployment", "payment-service-canary")
                    # Real bug found live (independent of the build stage's
                    # own version resolution): this hardcoded "v1.1.0"
                    # regardless of what version was actually requested for
                    # this run, so a deploy stage could apply a DIFFERENT
                    # image than the one the build stage just produced.
                    # Mirrors the build stage's own resolution above.
                    deploy_tag = target_version or config.get("imageTag", "v1.1.0").replace(
                        "{{ .TargetVersion }}", target_version or "v1.1.0"
                    )
                    self._log(run_id, f"Deploying canary: {deployment} -> tag {deploy_tag}")
                    # A generic project-onboarded deploy stage declares
                    # `image` (its real registry image — see
                    # generate_project_pipeline_yaml); the legacy hand-wired
                    # payments-service demo pipeline never has, so this keeps
                    # using its own hardcoded name/namespace/image exactly as
                    # before. See deploy_project_canary_task's docstring for
                    # the real gap this branch closes.
                    if config.get("image"):
                        res = deploy_project_canary_task(
                            run_id,
                            namespace=config.get("namespace", spec.namespace),
                            deployment_name=deployment,
                            container_name=config.get("containerName", service_name),
                            image_name=config["image"],
                            image_tag=deploy_tag,
                        )
                        last_deploy_image_name = config["image"]
                        last_deploy_image_tag = deploy_tag
                    else:
                        res = deploy_canary_task(run_id, deploy_tag, deployment)
                    results[stage_name] = res
                    self._log(run_id, f"Deploy finished: {res.get('status')}")

                elif stage_type == "canary_loop":
                    # Real gap found live (2026-09-15): a project's genuinely
                    # first-ever deployment has no prior version to compare
                    # against — running statistical verification against a
                    # "baseline" that's really just a placeholder (or, before
                    # this fix, an invalid image that never even started) is
                    # a category error. Matches Argo Rollouts' own documented
                    # first-deployment behavior: ship straight to 100% on
                    # both sides, skip analysis entirely, and only start real
                    # canary comparisons from the SECOND deployment onward,
                    # once a real previous-known-good version actually exists
                    # to protect. `pipeline_id` is None only for the one
                    # legacy hand-wired demo pipeline invoked without it,
                    # which keeps running the normal verified path exactly
                    # as before.
                    is_first_deploy = bool(
                        pipeline_id and self.db and self.db.run_from_thread(
                            self.db.is_first_deployment(pipeline_id, resolved_tenant_id)
                        )
                    )
                    if is_first_deploy:
                        self._log(
                            run_id,
                            "First-ever deployment for this project — no baseline to compare against yet; "
                            "shipping straight to 100% and skipping canary verification.",
                        )
                        if last_deploy_image_name:
                            deploy_cfg = next(
                                (s.get("config", {}) for s in spec.stages if s.get("type") == "deploy"), {}
                            )
                            baseline_deployment_name = (
                                config.get("baselineDeployment") or f"{service_name}-baseline"
                            )
                            deploy_project_canary_task(
                                run_id,
                                namespace=deploy_cfg.get("namespace", spec.namespace),
                                deployment_name=baseline_deployment_name,
                                container_name=deploy_cfg.get("containerName", service_name),
                                image_name=last_deploy_image_name,
                                image_tag=last_deploy_image_tag,
                            )
                            # Real gap found live: everything above patches
                            # the Deployments, but nothing told the real
                            # Gateway API HTTPRoute this project is now
                            # live — the platform's OWN state said "100%"
                            # while Kubernetes' actual routing object never
                            # changed. Still real-guardrail-checked (freeze
                            # windows) via OPA, not a raw unchecked patch —
                            # see set_first_deployment_route_weights's
                            # docstring for why this doesn't go through
                            # actuation_executor.py's verdict-gated path.
                            route_name = config.get("routeName") or f"{service_name}-route"
                            set_first_deployment_route_weights(
                                run_id,
                                namespace=deploy_cfg.get("namespace", spec.namespace),
                                route_name=route_name,
                                pipeline_policy={"gates": spec.gates, "guardrails": spec.guardrails},
                            )
                        self.db.run_from_thread(
                            self.db.mark_first_deployment_completed(pipeline_id, resolved_tenant_id)
                        )
                        # Real gap found live: nothing else in this branch
                        # ever updates `current_traffic_weight` (only
                        # policy-controller does, after a real verdict — see
                        # actuation_executor.py — which never runs for a
                        # first deployment). Without this, the frontend's
                        # traffic-weight chart would show a stale 0% forever
                        # for a run that's actually live at 100%. This
                        # updates the platform's OWN state record, not the
                        # real HTTPRoute (still a follow-up — see
                        # docs/session-notes/2026-09-15-onboarding-gaps-research.md);
                        # for a first deployment there is no statistical
                        # decision to gate, only the shape of the record.
                        initial_state.current_traffic_weight = 100
                        initial_state.last_updated = datetime.now(timezone.utc).isoformat()
                        self.state_store.save_state_sync(initial_state)
                        results[stage_name] = {"status": "first_deployment_promoted_without_verification"}
                        self._log(run_id, "Baseline aligned to the same image — future deployments will be verified canaries.")
                    else:
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
            self._request_stage_failure_rca(run_id, initial_state.current_stage, str(e))
            raise e
        finally:
            self.state_store.release_tenant_lock(resolved_tenant_id, service_name)
            if repo_clone_workspace:
                cleanup_workspace(run_id)

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
