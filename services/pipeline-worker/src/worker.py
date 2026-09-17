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
from src.tasks.dockerfile_synthesis import synthesize_dockerfile
from src.tasks.git_clone import cleanup_workspace
from src.tasks.deploy_task import (
    deploy_canary_task,
    deploy_project_canary_task,
    set_first_deployment_route_weights,
    wait_for_deployment_ready,
)
from src.tasks.ecr_auth import is_ecr_image, get_ecr_registry_credential, ensure_ecr_repository_exists
from src.tasks.verification_task import run_verification_task
from src.tasks.rollout_task import run_rollout_task
from src.schemas import parse_duration_seconds
from src.aws.ecs_deploy_task import (
    deploy_ecs_canary_task,
    deploy_ecs_baseline_task,
    wait_for_ecs_service_ready,
    set_first_deployment_ecs_weights,
    cutover_blue_green_ecs_weights,
    rollback_blue_green_ecs_weights,
    graduate_blue_green_ecs,
    compute_blue_green_cost,
)
from shared.aws_ecs_actuation import wait_for_target_group_healthy
from shared.live_url_check import verify_live_url
from shared.live_url_builder import build_live_url

logger = structlog.get_logger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
EXPLAINABILITY_SERVICE_URL = os.environ.get("EXPLAINABILITY_SERVICE_URL", "http://explainability-service:8004")
# Module 8 continuation — must match api-gateway's projects_router.py's own
# AWS_ALB_BASE_URL exactly (the real shared ALB's stable DNS name). Needed
# here, not just in api-gateway, because the blue-green cutover path below
# verifies the live URL for real BEFORE reporting a rollout as complete —
# api-gateway's own copy only ever computes the string for display.
AWS_ALB_BASE_URL = os.environ.get("AWS_ALB_BASE_URL")


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
            # Module 8 — real gap found live: policy-controller's ongoing
            # weight-shift/graduate/rollback actuation was hardcoded to
            # Kubernetes with no way to know a project's real deploy target.
            # `deploymentTarget`/`awsRegion`/`pathPrefix` are emitted onto
            # this same canary_loop config by generate_project_pipeline_yaml
            # for an "aws_ecs" project — reused here as-is (never guessed)
            # so policy-controller's actuation branch and this project's
            # real onboarded ALB listener rule can never drift apart.
            # canary_deployment_name/baseline_deployment_name double as the
            # real ECS service names for an AWS project too — both targets
            # derive them from the identical f"{service_name}-canary"/
            # "-baseline" convention (see ecs_manifest.py), so no separate
            # field is needed for that part.
            "deployment_target": canary_loop_cfg.get("deploymentTarget", "kubernetes"),
            "aws_region": canary_loop_cfg.get("awsRegion", "us-east-1"),
            "path_prefix": canary_loop_cfg.get("pathPrefix"),
            "deployment_strategy": canary_loop_cfg.get("deploymentStrategy", "canary"),
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
        # Set by the build stage — reused by the test stage as a hint for
        # WHICH requirements.txt is this run's own, when a repo has more
        # than one at equal depth (see build_task.py's
        # _find_requirements_file docstring for the real bug this guards).
        dockerfile: str | None = None

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
                    # Real gap found live: this only ever read `dockerfilePath`
                    # — a project onboarded via the wizard's YAML-manifest or
                    # auto-detected-language path (shared/repo_scanner.py,
                    # dockerfile_synthesis.py) worked for the onboarding-time
                    # PREVIEW build, but a real triggered rollout's build
                    # stage had no way to synthesize a Dockerfile at all, so
                    # it would fail here even though the exact same repo had
                    # already been proven buildable during onboarding.
                    # Mirrors build_preview.py's synthesis branch exactly —
                    # same templates, same "never guess a start command"
                    # rule — so preview and real rollout answer "does this
                    # build" with literally the same mechanism.
                    dockerfile_content = None
                    # Guaranteed Live Web App CI/CD — a static site (no
                    # process to start, nginx just serves files) and a Vite/
                    # CRA "spa" framework (built to static assets, also
                    # served by nginx) are the two real cases with no
                    # startCommand at all — requiring one for them would
                    # reject a perfectly buildable project.
                    no_start_command_needed = (
                        config.get("language") == "static" or config.get("framework") == "spa"
                    )
                    if config.get("dockerfilePath"):
                        dockerfile = config["dockerfilePath"]
                    elif config.get("language") and (config.get("startCommand") or no_start_command_needed):
                        manifest_path = config.get("manifestPath", "")
                        dockerfile_content = synthesize_dockerfile(
                            config["language"], manifest_path or "requirements.txt", config.get("startCommand"),
                            framework=config.get("framework"),
                        )
                        manifest_dir = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else ""
                        dockerfile = "/".join(p for p in [manifest_dir, "Dockerfile"] if p) or "Dockerfile"
                        self._log(run_id, f"No Dockerfile declared — synthesized one for {config['language']} at {dockerfile}")
                    else:
                        dockerfile = "sample-app/v1.1.0/Dockerfile"
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
                        aws_region = os.environ.get("AWS_REGION", "us-east-1")
                        # Real gap found live: unlike most registries, ECR
                        # doesn't auto-create a repository on first push — a
                        # project's very first build always failed the push
                        # unless someone had already run `aws ecr
                        # create-repository` by hand. Idempotent, so this is
                        # a no-op on every build after the first.
                        ensure_ecr_repository_exists(image_name, aws_region)
                        registry_credential = get_ecr_registry_credential(aws_region)

                    self._log(run_id, f"Building {dockerfile} -> tag {tag}")
                    res = run_build_task(
                        run_id,
                        dockerfile,
                        tag,
                        repo_config=repo_config,
                        image_name=image_name,
                        registry_credential=registry_credential,
                        dockerfile_content=dockerfile_content,
                    )
                    results[stage_name] = res
                    repo_clone_workspace = res.get("workspace")
                    if clone_token:
                        self.redis.delete(f"clone_token:{run_id}")
                    self._log(run_id, f"Build finished: {res.get('status')}")

                elif stage_type == "test":
                    # Real gap found live: this used to default to a
                    # Python-specific `pytest` command whenever a pipeline's
                    # test stage declared none, and ANY failure here — wrong
                    # language, no matching test files, the command itself
                    # missing on this container — hard-failed the whole
                    # pipeline. A project's own Dockerfile very often
                    # already runs its real tests as a build layer (e.g.
                    # `RUN npm run test` before the production build), which
                    # already gates the build genuinely — this separate
                    # stage runs in pipeline-worker's OWN container (no
                    # Node.js, no Go, etc.), so it can only ever be a
                    # supplementary, best-effort signal for whatever
                    # language IS runnable here, never a mandatory gate.
                    # No command configured -> skip outright, don't guess one.
                    test_cmd = config.get("command")
                    if not test_cmd:
                        self._log(run_id, "No test command configured — skipping (the Docker build itself may already run real tests).")
                        results[stage_name] = {"status": "skipped", "reason": "no test command configured"}
                    else:
                        self._log(run_id, f"Running: {test_cmd}")
                        requirements_subdir = dockerfile.rsplit("/", 1)[0] if dockerfile and "/" in dockerfile else None
                        try:
                            res = run_test_task(
                                run_id, test_cmd, cwd=repo_clone_workspace, requirements_subdir=requirements_subdir
                            )
                            results[stage_name] = res
                            self._log(run_id, f"Tests finished: {res.get('status')}")
                        except Exception as e:
                            logger.warning("test_stage_failed_non_blocking", run_id=run_id, error=str(e))
                            results[stage_name] = {"status": "failed_non_blocking", "error": str(e)}
                            self._log(run_id, f"Test stage failed (non-blocking, continuing to build/deploy): {e}")

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
                    if config.get("image") and config.get("deploymentTarget") == "aws_ecs":
                        # Module 8 — real gap found live: this branch didn't
                        # exist at all until now, so an "aws_ecs" project's
                        # real ECS canary service kept running whatever
                        # image:tag onboarding gave it at creation time,
                        # forever — a genuine git push built and pushed a
                        # new image to ECR that nothing ever deployed.
                        res = deploy_ecs_canary_task(
                            run_id,
                            service_name=config.get("containerName", service_name),
                            image=config["image"],
                            image_tag=deploy_tag,
                            region=config.get("awsRegion", "us-east-1"),
                        )
                        last_deploy_image_name = config["image"]
                        last_deploy_image_tag = deploy_tag
                    elif config.get("image"):
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
                    # Real gap found live (2026-09-16): a genuinely new web
                    # app with zero real visitors can never accumulate the
                    # samples PROMOTE_STEP's statistical gates require, so a
                    # project onboarded with deploy_mode: blue_green would
                    # sit DEGRADED ("insufficient samples") forever and never
                    # actually reach its live URL — even though the build and
                    # deploy themselves succeeded. Blue-green is deliberately
                    # its own strategy, checked BEFORE is_first_deploy and
                    # applying to EVERY rollout (not just the first — this is
                    # a deployment-strategy choice, not a one-time exception):
                    # it never runs statistical verification at all, gating
                    # the cutover on real infrastructure health instead
                    # (ECS task stability + a real ALB HTTP liveness check),
                    # the same category of gate the first-deployment branch
                    # below already established as legitimate.
                    is_blue_green = (
                        config.get("deploymentStrategy") == "blue_green"
                        and config.get("deploymentTarget") == "aws_ecs"
                    )
                    if is_blue_green:
                        ecs_service_name = config.get("service", service_name)
                        aws_region = config.get("awsRegion", "us-east-1")
                        path_prefix = config.get("pathPrefix") or f"/api/v1/{ecs_service_name}"
                        live_url = build_live_url(AWS_ALB_BASE_URL, path_prefix)
                        blue_green_policy = {"gates": spec.gates, "guardrails": spec.guardrails}

                        self._log(run_id, "Blue-green rollout — waiting for the new (green) ECS service to stabilize...")
                        wait_for_ecs_service_ready(aws_region, ecs_service_name, "canary")
                        self._log(run_id, "Waiting for the green target group to report healthy via a real ALB health check...")
                        wait_for_target_group_healthy(aws_region, f"{ecs_service_name}-canary")
                        self._log(run_id, "Green is healthy — cutting over 100% of traffic (no statistical verification needed).")
                        cutover_blue_green_ecs_weights(
                            run_id, service_name=ecs_service_name, path_prefix=path_prefix,
                            region=aws_region, pipeline_policy=blue_green_policy,
                        )
                        # Real gap found live (2026-09-17): this whole
                        # blue-green path never wrote a single audit_ledger
                        # row — the Audit Ledger UI correctly showed "0
                        # total actions" for a run that had genuinely cut
                        # over real production traffic, because nothing
                        # here ever called record_actuation (unlike the
                        # verdict-driven canary path, which always does via
                        # policy-controller's audit_writer.py).
                        if self.db:
                            self.db.run_from_thread(
                                self.db.record_actuation(
                                    tenant_id=resolved_tenant_id, pipeline_run_id=run_id,
                                    action="BLUE_GREEN_CUTOVER", canary_weight=100, baseline_weight=0,
                                    authorized_by="OPA:rule=HEALTH_GATED_CUTOVER",
                                )
                            )
                            # Real gap found live (2026-09-17): cost_analysis
                            # had 0 rows for any blue-green rollout ever —
                            # the Reports & Cost UI and RULE 7's guardrail
                            # both read from a table only the verdict-driven
                            # controller.py ever wrote to. Fail-soft: a cost
                            # snapshot failing must never fail the rollout.
                            cost_guardrail = spec.guardrails.get("maxPermittedCostDeltaPercent", 15.0)
                            cost_result = compute_blue_green_cost(ecs_service_name, aws_region, cost_guardrail)
                            if cost_result:
                                self.db.run_from_thread(
                                    self.db.record_cost_analysis(
                                        tenant_id=resolved_tenant_id, pipeline_run_id=run_id,
                                        baseline_cost=cost_result["baseline_cost_usd"],
                                        canary_cost=cost_result["canary_cost_usd"],
                                        delta_percent=cost_result["delta_percent"],
                                    )
                                )

                        verify_result = (
                            verify_live_url(live_url) if live_url
                            else {"verified": False, "status_code": None, "error": "AWS_ALB_BASE_URL not configured"}
                        )
                        if pipeline_id and self.db:
                            self.db.run_from_thread(
                                self.db.record_live_url_verification(
                                    pipeline_id, resolved_tenant_id, verify_result["verified"]
                                )
                            )
                        if not verify_result["verified"]:
                            self._log(
                                run_id,
                                f"Live URL verification failed after cutover ({verify_result.get('error')}) — "
                                "rolling back to the previous version.",
                            )
                            rollback_blue_green_ecs_weights(
                                run_id, service_name=ecs_service_name, path_prefix=path_prefix, region=aws_region,
                            )
                            if self.db:
                                self.db.run_from_thread(
                                    self.db.record_actuation(
                                        tenant_id=resolved_tenant_id, pipeline_run_id=run_id,
                                        action="ROLLBACK", canary_weight=0, baseline_weight=100,
                                        authorized_by="SYSTEM:blue_green_post_cutover_live_url_verification_failed",
                                    )
                                )
                            raise RuntimeError(
                                f"Blue-green cutover for '{ecs_service_name}' failed live-URL verification "
                                f"({verify_result.get('error')}) — rolled back to the previous version."
                            )

                        self._log(run_id, "Live URL verified — graduating: promoting the new image onto baseline.")
                        graduate_blue_green_ecs(
                            run_id, service_name=ecs_service_name, image=last_deploy_image_name,
                            image_tag=last_deploy_image_tag, path_prefix=path_prefix, region=aws_region,
                        )
                        if self.db:
                            self.db.run_from_thread(
                                self.db.record_actuation(
                                    tenant_id=resolved_tenant_id, pipeline_run_id=run_id,
                                    action="GRADUATE", canary_weight=0, baseline_weight=100,
                                    authorized_by="SYSTEM:blue_green_graduate",
                                )
                            )
                        initial_state.current_traffic_weight = 100
                        initial_state.last_updated = datetime.now(timezone.utc).isoformat()
                        self.state_store.save_state_sync(initial_state)
                        results[stage_name] = {"status": "blue_green_cutover_verified_and_graduated"}
                        self._log(run_id, "Blue-green rollout complete — live and verified.")
                        continue

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
                        if last_deploy_image_name and config.get("deploymentTarget") == "aws_ecs":
                            # Module 8 — AWS ECS equivalent of the Kubernetes
                            # first-deployment branch below: deploy baseline
                            # with the same image the canary deploy stage
                            # just pushed, wait for BOTH real ECS services to
                            # actually stabilize (the direct equivalent of
                            # wait_for_deployment_ready) before cutting real
                            # ALB traffic over — an unhealthy first AWS
                            # deployment must never get traffic either.
                            ecs_service_name = config.get("service", service_name)
                            aws_region = config.get("awsRegion", "us-east-1")
                            deploy_ecs_baseline_task(
                                run_id, ecs_service_name, last_deploy_image_name, last_deploy_image_tag, aws_region,
                            )
                            self._log(run_id, "Waiting for the new ECS services to become stable before cutting over traffic...")
                            for cohort in ("canary", "baseline"):
                                wait_for_ecs_service_ready(aws_region, ecs_service_name, cohort)
                            self._log(run_id, "ECS services stable — cutting over traffic to 100%.")
                            path_prefix = config.get("pathPrefix") or f"/api/v1/{ecs_service_name}"
                            set_first_deployment_ecs_weights(
                                run_id,
                                service_name=ecs_service_name,
                                path_prefix=path_prefix,
                                region=aws_region,
                                pipeline_policy={"gates": spec.gates, "guardrails": spec.guardrails},
                            )
                            # Guaranteed Live Web App CI/CD — real gap this
                            # closes: everything above confirms the ECS
                            # tasks are RUNNING and shifts the ALB weight,
                            # but nothing ever confirmed a real visitor's
                            # request through the real ALB actually gets a
                            # response — the exact "healthy target group,
                            # 404 through the real URL" class of bug (ALB
                            # path-prefix trap, see CLAUDE.md) that
                            # target-group health alone can't catch. Not
                            # gated (unlike blue-green's own check): a
                            # first deployment has no prior known-good
                            # version to roll back TO, so a failed check
                            # here is recorded and logged, not fatal.
                            if AWS_ALB_BASE_URL:
                                verify_result = verify_live_url(build_live_url(AWS_ALB_BASE_URL, path_prefix))
                                if pipeline_id and self.db:
                                    self.db.run_from_thread(
                                        self.db.record_live_url_verification(
                                            pipeline_id, resolved_tenant_id, verify_result["verified"]
                                        )
                                    )
                                if verify_result["verified"]:
                                    self._log(run_id, "Live URL verified — a real request through the ALB got a response.")
                                else:
                                    self._log(
                                        run_id,
                                        f"Live URL verification did not succeed ({verify_result.get('error')}) — "
                                        "the deployment is live per ECS/ALB state, but check the app's routing "
                                        "(e.g. does it handle the assigned path prefix?).",
                                    )
                        elif last_deploy_image_name:
                            deploy_cfg = next(
                                (s.get("config", {}) for s in spec.stages if s.get("type") == "deploy"), {}
                            )
                            deploy_namespace = deploy_cfg.get("namespace", spec.namespace)
                            baseline_deployment_name = (
                                config.get("baselineDeployment") or f"{service_name}-baseline"
                            )
                            canary_deployment_name = config.get("canaryDeployment") or f"{service_name}-canary"
                            deploy_project_canary_task(
                                run_id,
                                namespace=deploy_namespace,
                                deployment_name=baseline_deployment_name,
                                container_name=deploy_cfg.get("containerName", service_name),
                                image_name=last_deploy_image_name,
                                image_tag=last_deploy_image_tag,
                            )
                            # Real gap found live: everything above patches
                            # the Deployment objects, but nothing ever
                            # confirmed a pod actually came up healthy before
                            # cutting real traffic over — "patched" and
                            # "serving traffic" were silently treated as the
                            # same thing. wait_for_deployment_ready raises on
                            # timeout, which propagates to this pipeline's
                            # normal failure path (FAILED + real RCA) and
                            # deliberately happens BEFORE route weights ever
                            # change and BEFORE mark_first_deployment_completed
                            # — an unhealthy first deployment never gets
                            # traffic, and a retry is still correctly treated
                            # as a first deployment, not silently marked done.
                            self._log(run_id, "Waiting for the new deployment to become healthy before cutting over traffic...")
                            for name in (canary_deployment_name, baseline_deployment_name):
                                wait_for_deployment_ready(run_id, namespace=deploy_namespace, deployment_name=name)
                            self._log(run_id, "Deployment is healthy — cutting over traffic to 100%.")
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
                                namespace=deploy_namespace,
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
                            run_id, spec.verificationConfig, trace_id=trace_id, tenant_id=resolved_tenant_id,
                            deployment_target=config.get("deploymentTarget", "kubernetes"),
                            aws_region=config.get("awsRegion"),
                            ecs_service_name=config.get("service"),
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
