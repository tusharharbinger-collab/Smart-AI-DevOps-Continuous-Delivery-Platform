# Project Status — Smart AI DevOps & Continuous Delivery Platform

Last updated: 2026-09-16. This file is a living tracker — update it as work
completes and is *verified live*, not when code merely exists (same
operating principle as `docs/roadmap/00-ROADMAP.md`). It exists to answer
one question at a glance: **what actually works today, and what's left**,
across the whole project — the original assignment spec, the 8 completed
product-hardening phases, and the currently active Phase 9 build-out.

## Main goal

> A user connects a GitHub repo. **Before anything goes live**, they walk
> through Build → Test → Deploy once, human-confirmed at each step, with
> the platform explaining — in plain language, AI-assisted where it adds
> value — exactly what it found and why, especially when something fails.
> That one guided pass establishes the **base model**: the first real,
> live, running version. **Every version after that is fully autonomous**:
> a `git push` triggers build → test → statistical verification against
> the live version → cutover to the cloud, entirely without a human,
> unless it isn't safe — in which case it blocks and explains why, never
> silently. The platform can do this on Kubernetes, AWS ECS, AWS EKS, or
> (later) Render/Vercel/Railway/Azure — the human picks the target once,
> the mechanism adapts.
>
> Once that pipeline exists, **its main ongoing value is what it tells the
> user about the running system**: is it healthy, what is it costing,
> what's wrong and how to fix it (AI-generated), and — most important of
> all — how rollout decisions are actually being made and enforced. Every
> verification decision is a real statistical test against real evidence,
> never a static threshold; every traffic shift and every rollback is
> cryptographically signed, policy-gated, and auditable.

This is the same vision stated in `docs/roadmap/09-universal-delivery-platform.md`,
re-prioritized: Reports/Cost/Rollout visibility is the payoff the user
actually cares about; the build/test/deploy walkthrough and the autonomous
update loop are what has to exist underneath it for that visibility to mean
anything.

---

## Quick-glance status

| Area | Status |
|---|---|
| Core platform (5 services, statistical verification, signed verdicts, OPA guardrails, RLS multi-tenancy, real auth) | ✅ Done |
| Real Kubernetes target (Kind + Envoy Gateway) | ✅ Done, live-verified |
| Real AWS ECS + Fargate target | ✅ Done, live-verified (onboarding + ongoing rollout) |
| Real AWS EKS target | ✅ Done once, live-verified, then torn down to stop billing — recreation runbook exists |
| Pre-deploy Build/Test/Deploy wizard experience | ⚠️ Mostly done, as wizard panels not dedicated pages |
| Post-deploy insights (health/cost/AI solutions) UI | ✅ Reports + Cost tabs done and live-verified (2026-09-16); CloudWatch telemetry now real too (2026-09-16) — right-sizing UI is honest-but-dataless only because nothing calls the (now unblocked) computation yet, see P1 #5 |
| Blue-green deployment mode | ⚠️ Code-complete + unit-tested (2026-09-17), NOT yet live-verified against real AWS — see "Guaranteed Live Web App CI/CD" section below |
| Universal Dockerfile synthesis (SPA/Next.js/static, health-check defaults, live-URL verification) | ⚠️ Code-complete + unit-tested (2026-09-17), NOT yet live-verified — see below |
| Shared `DeploymentTarget` abstraction | ❌ Not started (2 real targets exist, not unified) |
| Autonomous git-push-triggered CI/CD loop | ✅ Trigger + gate 1 + gate 2 + polling fallback all done and live-verified end to end (2026-09-16) |
| Local → global platform deployment (hosting the platform itself) | ❌ Not started |

---

## Deploy-target decision (2026-09-16)

Decided while scoping the "push any project to the cloud, real CI/CD,
real URL, reports/cost as the main payoff" goal:

- **Kind is disqualified for "real hosting."** It runs on one laptop —
  even with every local bug fixed, its `live_url` only ever resolves on
  that machine. It stays useful for free local iteration before pushing
  to a real target, but it is not a candidate for what the user actually
  gets a link to.
- **AWS ECS Fargate is the primary build target going forward.** It's
  the more complete, more recently live-verified path (onboarding *and*
  every ongoing rollout, not just the first deploy), and it's cheaper to
  leave genuinely always-on than EKS's continuous control-plane billing.
  Its one real gap was cost tracking (see 9.1 below) — done 2026-09-16.

**Update (2026-09-16): EKS/Kubernetes is now explicitly deprioritized, not
just "not the primary."** Explicit decision: focus exclusively on AWS
ECS going forward — all Kubernetes/Kind/EKS-specific backlog work (the
deployment-target-unification interface, in-cluster Prometheus telemetry,
the `eksctl` teardown bug, the HTTPRoute 409-conflict bug) removed from
`BACKLOG.md`. **Known, accepted tradeoff, not an oversight:** EKS was the
only target satisfying `MASTER_BUILD_SPEC.md`'s graded rubric requirement
for Kubernetes/Gateway API traffic-splitting (§5.1/§5.3) — this choice
trades that rubric line item for a simpler, single-target backlog. The
Kind/EKS code itself is untouched and still real/working (nothing was
deleted) if this decision is revisited later; `EKS_CLUSTER_NAME` still
works exactly as documented above.

---

## Part 1 — Assignment spec (`MASTER_BUILD_SPEC.md`)

| Item | Status |
|---|---|
| All 5 backend services + frontend, real source/Dockerfiles, `docker compose up --build` → 16 healthy containers | ✅ Done |
| Statistical verification engine (SPRT, Mann-Whitney/KS, CUSUM/BOCPD, Fisher/χ², Isolation Forest) | ✅ Done |
| Signed verdicts (HMAC), OPA guardrails, adversarial tests | ✅ Done |
| Postgres RLS multi-tenancy | ✅ Done |
| `tests/e2e/*.py` | ❌ Missing (spec Phase 10) |
| `scripts/demo/*.sh` (`make demo-healthy` / `make demo-fail`) | ✅ **Done and live-verified 2026-09-18** — `demo_failed_rollout.sh` flips the v1.1.0 sample-app's real `INJECT_ERRORS` env var on the live Kind canary Deployment (genuine 2.0% error rate + 65ms latency spike, built to trip SPRT/CUSUM for real), `demo_healthy_rollout.sh` resets it; both verify the change against the live Deployment spec, not just that `kubectl` returned 0. Deliberately does **not** trigger the rollout itself — that stays a manual "Trigger New Rollout" click so a live audience sees a real click start it. |
| Celery for `pipeline-worker` | ⚠️ Deviation — sync function calls + Redis Streams consumer groups instead (functionally equivalent, spec names Celery) |
| MinIO | ⚠️ Container up, nothing in the app writes to it |
| RCA generation (Groq) | ⚠️ Real, but only verified via deterministic fallback unless `GROQ_API_KEY` is configured live |
| ChatOps query interface (bonus item — "a query interface that still grounds its answer in the real comparison data") | ✅ **Done 2026-09-18.** See 9.7 below. |

## Part 2 — Product roadmap, Phases 1–8

| # | Phase | Status |
|---|---|---|
| 1 | Frontend Overhaul | ✅ Done |
| 1b | "Autonoma" Visual Refresh | 🚧 In progress |
| 2 | Real Telemetry (Prometheus, docker-compose scope) | ✅ Done |
| 3 | Multi-Service Onboarding | ✅ Done |
| 4 | Security Hardening | ✅ Mostly done — TLS termination + real secrets store deferred to Phase 7 |
| 5 | Reliability & Scale (Redis Streams, crash recovery) | ✅ Done |
| 6 | Platform Observability (Loki/Promtail/Grafana, tracing, self-health alerts) | ✅ Done — OpenTelemetry spans deferred (stretch goal) |
| 7 | Deployment (Local → Global) — hosting the *platform itself* publicly | ❌ Not started |
| 8 | Project Workspaces & GitHub Delivery | ✅ Done — `stage_logs` persistence and the historical stage-DAG fallback fixed and live-verified 2026-09-18 (see §9.7's sibling note below) |

## Part 3 — Phase 9: Universal Delivery Platform (active work)

### 9.1 — Reports & Cost pages
| Item | Status |
|---|---|
| `GET /reports/digest/{tenant_id}` (success/failure rate, rollback frequency, MTTV, cost trend) | ✅ Backend done |
| `GET /reports/deployment/{run_id}` (baseline-vs-canary comparison, verdict, evidence) | ✅ Backend done |
| **`/projects/:id/reports` tab (frontend)** | ✅ **Done and live-verified 2026-09-16.** `ReportsTab.tsx` — tenant-wide digest (stat cards) + AI summary card + a per-project run picker driving the existing deployment-report view. `tsc -b` and `vite build` both clean. Live-verified: real login, real digest fetch showing a genuine Groq-generated summary (not the fallback — `GROQ_API_KEY` is live-configured) against 20 real deployments. |
| **`/projects/:id/cost` tab (frontend)** | ✅ **Done and live-verified 2026-09-16.** `CostTab.tsx` — reads the new `GET /{project_id}/cost-history` endpoint. Live-verified against the real `payments-pipeline` project's actual `cost_analysis` rows, exact field-for-field match with the TypeScript interface. |
| Per-pipeline cost breakdown (`cost_tracker.py`) | ✅ Real for Kubernetes — reads live CPU/memory *requests* off the real Deployment spec, multiplies by a configurable `$/vCPU-hr` + `$/GiB-hr` rate (a genuine resource-spec estimate, not an AWS billing-API call). ✅ **ECS equivalent done and live-verified 2026-09-16** (`cost_tracker_ecs.py`, wired into `controller.py`, 24 new tests + full 98/98 policy-controller suite passing, real Fargate pricing confirmed against `aws.amazon.com/fargate/pricing/`) — run live against the real `smartcd-platform` ECS cluster (account `236087863083`) against two real projects (`payments-aws`, `raktdoot`), correct dollar figures confirmed by hand-calculation. **New `GET /{project_id}/cost-history` endpoint (2026-09-16)** exposes this per-run, project-scoped — 5 new tests, live-verified against real rows. |
| **AI-generated digest summary** ("why did rollback rate go up this month") | ✅ **Done and live-verified 2026-09-16.** `digest_summarizer.py` (explainability-service) — same Groq call shape as `report_generator.py`/`stage_failure_analyzer.py` (hard timeout, JSON-object response, deterministic fallback), wired into `GET /digest/{tenant_id}`. 7 new tests. Live-verified: a real Groq call produced a genuine, grounded summary against real digest numbers. |
| **AI right-sizing recommendations surfaced to a human for approval** | ⚠️ **UI built and honest (2026-09-16); the AWS ECS data blocker is now cleared, but nothing calls the computation yet.** `cost_analyzer.py::compute_rightsizing_recommendation` is real and already unit-tested, and `cloudwatch_client.py::fetch_saturation_samples` (done 2026-09-16, live-verified above) now provides genuine observed CPU/memory utilization for any AWS ECS project — the real gap left is wiring a caller that feeds those samples into `compute_rightsizing_recommendation` and persists the result; Kubernetes projects still have no equivalent (in-cluster Prometheus telemetry, 9.5, still not started). `rightsizing_rec` is genuinely NULL for every row today. The Cost tab renders it if present and an honest "not enough usage data yet" otherwise — deliberately not fabricated, matching `cost_tracker.py`'s own stated principle. |

### 9.2 — Per-stage build/test/deploy pages
| Item | Status |
|---|---|
| Repo picker: Dockerfile / `smartcd.yaml` / auto-detect, confidence-tagged checklist | ✅ Done — "any project" is bounded to a repo that has its own `Dockerfile`/`smartcd.yaml`, **or** is Python/Node/Go (the only languages `dockerfile_synthesis.py` currently templates); other languages need a supplied `Dockerfile` today |
| Build & Test dry run, live logs, pass/fail banner | ✅ Done, live-verified both ways (Dockerfile present / synthesized) |
| **AI-narrated build/test failure explanation** | ✅ **Done and live-verified 2026-09-16** for the gate-1 autonomous path — `report_gate1_result` now calls explainability-service's already-existing, already-tested `/stage-failure-rca` endpoint (the same one `worker.py::_request_stage_failure_rca` uses for a real pipeline stage failure) with the real preview logs, storing the grounded RCA alongside the block reason. 7 new tests. The wizard's onboarding-time dry-run banner (rule-based human-side/platform-side classification) is unchanged — that's a different, already-adequate UX moment, not this item's scope. |
| Deploy stage: freeze windows, manual-approval roles, deploy-mode selector | ✅ Done, OPA-enforced, live-verified |
| `smartcd.yaml` manifest support (scanner + synthesis) | ✅ Done, including subfolder-path fix |
| Build/Test/Deploy as **dedicated routes** (not wizard-step panels) | ❌ Not started |
| Deployment-target picker in the wizard | ✅ **Simplified 2026-09-16** — the Kubernetes-vs-AWS toggle in `NewProject.tsx` is gone; every new project defaults straight to `aws_ecs` (Kubernetes/Kind code paths are untouched, just no longer offered from onboarding) |

### 9.3 — One-time "base model" guided deployment
| Item | Status |
|---|---|
| UI framing (first-deployment banner, liveness phase) | ✅ Done |
| Real post-deploy liveness check (`wait_for_deployment_ready`, polls real `status.readyReplicas`) | ✅ Done, live-verified (healthy + unhealthy cases) |
| Real, clickable `live_url` | ✅ Done (migration `0012`, `GATEWAY_BASE_URL`/`AWS_ALB_BASE_URL`) |
| HTTPRoute cross-namespace routing bug | ✅ Fixed — was silently breaking traffic-splitting for every project all session |
| Deprovisioning on project delete (K8s + AWS, no orphaned resources) | ✅ Done |

### 9.4 — Blue-green deployment mode ("Guaranteed Live Web App CI/CD", 2026-09-17)
**Root problem this closes:** a genuinely new web app with zero real visitors
can never accumulate the samples statistical canary verification needs, so
it would sit `DEGRADED` ("insufficient samples") forever and never reach
its live URL — even though the build/deploy themselves succeeded. The
pre-existing `RULE 10: BLUE_GREEN_CUTOVER` (verdict-gated, requires
`minSampleSize`) didn't actually fix this — it was built for a
statistically-informed cutover, not a zero-traffic guarantee, and is left
in place, dormant/unreachable for blue-green projects, rather than removed.
The real fix follows the platform's own existing precedent for this
category of problem: `FIRST_DEPLOYMENT`'s "skip statistical verification,
gate on real infrastructure health instead" pattern.

| Item | Status |
|---|---|
| `HEALTH_GATED_CUTOVER` OPA rule (freeze-window-only, no verdict dependency — a NEW rule, not a reuse of RULE 10) | ⚠️ Code-complete, 4 new OPA tests passing (11/11 total), NOT live-verified |
| `wait_for_target_group_healthy` (real ALB HTTP liveness check, `shared/aws_ecs_actuation.py`) | ⚠️ Code-complete, 4 unit tests, NOT live-verified |
| `cutover_blue_green_ecs_weights`/`rollback_blue_green_ecs_weights`/`graduate_blue_green_ecs` (`ecs_deploy_task.py`) | ⚠️ Code-complete, 6 unit tests, NOT live-verified |
| `worker.py`'s `is_blue_green` canary_loop branch — deploy → wait-stable → wait-healthy → OPA gate → cutover → live-URL verify → rollback-on-fail → graduate, statistical verification never invoked | ⚠️ Code-complete, 3 orchestration tests (healthy/unhealthy-target/failed-post-cutover-verify), NOT live-verified |
| `deploy_mode: blue_green` accepted by `create_project` (AWS ECS only — rejects it for Kubernetes with a 422) + emitted in generated YAML | ⚠️ Code-complete, 16 tests, NOT live-verified |
| Migration `0014` — `projects.deploy_mode`/`live_url_status`/`live_url_verified_at` | ⚠️ Not yet applied to any real database |
| Wizard deploy-mode toggle (Canary vs. Blue-Green), deploy-mode + live-URL-status badges (`ProjectsOverview.tsx`/`ProjectWorkspace.tsx`) | ⚠️ Code-complete, `tsc -b` + `vite build` both clean, NOT exercised in a real browser |
| Post-cutover automatic rollback | ✅ Included in the above — a failed post-cutover `verify_live_url` reverts weights and fails the pipeline; graduation never runs |
| **Universal Dockerfile synthesis** (Vite/CRA "spa", Next.js, static-HTML — `dockerfile_synthesis.py` + `repo_scanner.py`'s new `framework`/`language="static"` detection; fixed a real `npm install --omit=dev` bug that broke every Vite/Next build) | ⚠️ Code-complete, 12 synthesis tests + 23 detection tests, NOT live-verified (`docker build` never actually run against a real repo) |
| **Health-check path/matcher defaults** (`suggest_networking_defaults` — "/" for a web-facing app, "/healthz" kept for an API-style one; ECS target group `Matcher: 200-399`) | ⚠️ Code-complete, 5 + 2 tests, NOT live-verified |
| **Live-URL verification** (`shared/live_url_check.py::verify_live_url` — a real HTTP GET through the real ALB, not just target-group health) wired into blue-green cutover, first-deployment ECS, and canary-ramp graduation (`graduate_canary_ecs`) | ⚠️ Code-complete, 6 + 3 + 3 tests, NOT live-verified. Deliberately NOT wired into the Kubernetes/Kind first-deployment path — Kind is already disqualified for "real hosting" (see the Deploy-target decision above), so a live-URL check from inside a container wouldn't be meaningful there |

**Test summary:** 432 backend tests + 11 OPA tests passing (zero
regressions across every existing suite), `tsc -b` and `vite build` both
clean. **What's explicitly NOT done:** no real `docker build`/AWS
onboarding/cutover has been run — this entire body of work is unit-tested
against fakes/mocks only. Per this file's own stated principle ("update
when finished AND live-verified... don't mark something done from code
existing alone"), do not upgrade any row above to ✅ until it's been proven
against the real `smartcd-platform` AWS account the way every other ✅ row
in this document was.

### 9.5 — Deployment-target abstraction
| Item | Status |
|---|---|
| Kubernetes (Kind) target | ✅ Done, live-verified |
| **AWS ECS + Fargate target** | ✅ Done, live-verified end to end — onboarding AND ongoing rollout (build→deploy→canary→promote→graduate/rollback) |
| **AWS EKS target** | ✅ Done once, live-verified (real cluster, Envoy Gateway, live baseline/canary) — currently torn down to stop billing; `make eks-up` runbook exists to recreate |
| Shared `DeploymentTarget` interface (`deploy()`/`shift_traffic()`/`rollback()`/`read_cost_metrics()`) | ❌ Not started — ECS and EKS/Kind are real but parallel, hand-written code, not unified |
| **CloudWatch telemetry for AWS-deployed projects** | ✅ **Done and live-verified 2026-09-16.** `telemetry/cloudwatch_client.py` (verification-engine) — the CloudWatch analog of `prometheus_client.py`, deriving real ALB/ECS queries from this platform's own fixed naming convention (no per-project query config needed, unlike Prometheus). Wired through `main.py`'s `/verify` (`use_cloudwatch`), `pipeline-worker`'s `verification_task.py`/`worker.py`/`main.py` reverify endpoint, and `policy-controller`'s `rollout_scheduler.py` (both the first-verdict and every-subsequent-step reverify paths). **Three real bugs found and fixed by this session's own test-writing** (not hypothetical): (1) `resolve_target_group_dimension`'s ARN-suffix extraction was wrong — target group ARNs have one fewer path segment than load-balancer ARNs, so the shared logic left the whole `arn:aws:...:account-id:` prefix glued onto the dimension, guaranteeing every CloudWatch query would resolve to a dimension value that matches no real metric; (2) `main.py`'s `_fetch_metric_from_cloudwatch` checked `if not cw_cfg:`, but every real project emits `cloudwatch: {}` — an empty dict is falsy in Python, so this treated every real config as absent and skipped it, meaning the feature could never have fired for any real project; (3) the identical falsy-empty-dict bug in `verification_task.py`'s `use_cloudwatch` decision. 32 new tests (15 `cloudwatch_client.py`, 8 `main.py` dispatch, 4 `verification_task.py`, 5 `rollout_scheduler.py` target-threading — plus fixed 5 pre-existing `rollout_scheduler`-mocking tests broken by the earlier `target` parameter addition) — full regression suites all green (verification-engine 106/106, pipeline-worker 145/145, policy-controller 102/102, api-gateway 105/105). **Live-verified against the real `smartcd-platform` AWS account**: rebuilt the verification-engine image (added `boto3`), added its missing `AWS_REGION`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars to docker-compose, then called the real running `/verify` endpoint with `use_cloudwatch=true` against the real `payments-aws-baseline`/`payments-aws-canary` target groups — real `elbv2:DescribeTargetGroups`/`cloudwatch:GetMetricData` calls returned genuine, distinct CPUUtilization datapoints (baseline ~45.5-46.2%, canary ~70.0-73.2%), correctly triggering a real CUSUM saturation-regression detection and a real cross-metric Isolation Forest anomaly flag — zero changes needed to `engine.py` or any statistical test module. A nonexistent target group (`checkout-baseline`) correctly produced a real `TargetGroupNotFound` AWS error, caught and logged as `CloudWatchQueryError`, degrading confidence rather than crashing — matching the identical fail-soft contract the Prometheus path already has. `business_metric` is deliberately unsupported (no generic ALB/ECS-level signal exists for a business outcome) — this also fully unblocks the AI right-sizing recommendation once enough real saturation history accumulates. |
| In-cluster Prometheus (Helm) scraping real pods, labeled by cohort | ❌ Not started on either Kind or EKS — `monitoring/prometheus.yml` only scrapes docker-compose containers |
| Project-wizard pipeline YAML → real Prometheus wiring | ❌ Not started — every wizard-created project's pipeline YAML has zero `prometheus` query blocks today |
| Render / Vercel / Railway / Azure targets | ❌ Not started |

### 9.6 — Continuous autonomous update loop
| Item | Status |
|---|---|
| **GitHub webhook receiver** (signature verification, `X-GitHub-Delivery` dedup) | ✅ **Done and live-verified 2026-09-16.** `webhooks_router.py` (`POST /api/v1/webhooks/github`) + `webhook_registry.py` (Redis-backed repo→tenant/project resolution — see its docstring for why RLS makes this a Redis, not Postgres, lookup). `_trigger_rollout_internal` extracted from `trigger_project_rollout` so both paths share byte-identical actuation. 44 new tests, full api-gateway suite 83/83 passing. Live-verified against the real running stack: correct/incorrect HMAC signatures, `ping`, delivery dedup (confirmed via real `redis-cli GET`), and a genuine end-to-end happy path against the real `test-` project — produced a real `pipeline_executions` row (`trigger_type='GITHUB_PUSH'`) and a real `stream:pipeline:start` XADD with the project's actual pipeline YAML. |
| **Webhook auto-registration** (`POST /{project_id}/webhook/register`) | ✅ Done 2026-09-16 — creates the real GitHub-side webhook subscription via the user's OAuth token (`repo` scope already covers hooks, confirmed against GitHub's own scope docs), idempotent (reuses an existing hook pointed at our URL instead of duplicating). Unit-tested with `httpx.MockTransport`; not live-tested against real GitHub (would need a real public URL + a real OAuth session). |
| Delivery-processing failure does not permanently swallow a retry | ✅ Done — a mid-processing failure releases the dedup claim so GitHub's own retry (or a manual "Redeliver") is reprocessed, not silently dropped; live-verified. |
| **Polling fallback** (safety net for a delivery GitHub never retried, or the platform being down when it tried) | ✅ **Done and live-verified 2026-09-16.** `poll_for_missed_webhook_deliveries` (`webhooks_router.py`), wired as a periodic loop in api-gateway's `main.py` lifespan (15 min default, `WEBHOOK_POLL_INTERVAL_SECONDS`). Enumerates connected repos via Redis (`webhook:repo:*` — never Postgres, for the same RLS reason `webhook_registry.py` already documents), compares each repo's real GitHub branch HEAD against the last commit actually checked (`gate1_last_result:{project_id}.commit_sha`), and queues the identical gate1 check a real webhook would have via a new shared `_queue_gate1_check` helper (extracted so the real-time and polling paths can never drift). 5 new tests. Live-verified: real import chain, real `main.py` startup with the loop wired in. |
| **Gate 1: build+test on the new commit before any deploy attempt** | ✅ **Done and live-verified 2026-09-16.** New `stream:gate1:check` Redis Stream — the webhook receiver now queues a check instead of triggering directly; pipeline-worker's new consumer (`_process_gate1_check_message`, `_consume_gate1_check`, `_reclaim_stale_gate1_pending_loop`, mirroring the existing pipeline-start consumer's reliability shape) runs the real `build_preview.py` dry run and calls back into new `POST /internal/{project_id}/gate1-result`, which triggers the real rollout only on `passed=True` (`_trigger_rollout_internal`, byte-identical to a human trigger) — a `passed=False` result never touches the real pipeline, matching `build_preview.py`'s own "never confused for a real pipeline_execution" invariant. 9 new tests (api-gateway + pipeline-worker), full suites 87/87 and 141/141 passing. **Live-verified against the real `test-` project**: a real webhook push → real gate1 consumer clone+build+pytest run (genuinely passed) → real callback → real new `pipeline_executions` row (`trigger_type='GITHUB_PUSH'`) → the real existing canary pipeline picked it up and began executing — confirmed the gate1 check's own run_id never appears in `pipeline_executions` (by design). |
| Gate 2: statistical verification vs. live version, auto-promote/auto-rollback | ✅ Already real — the existing verification+OPA+actuation chain, unmodified, now reachable from a webhook-triggered run too, live-verified as part of the above |
| `UNVERIFIABLE` → wait, don't guess (traffic-floor protection) | ✅ Already an existing invariant (`minSampleSize`/`minDuration`) |

### 9.7 — ChatOps query interface (2026-09-18)

| Item | Status |
|---|---|
| `explainability-service`: `POST /chatops/ask` — assembles real grounding context (last N `pipeline_executions` joined via `COALESCE(execution_state.status, pipeline_executions.status)`, `verification_records`+`cost_analysis`, `audit_ledger`) and answers via Groq, structurally identical to `report_generator.py`'s established pattern (hard 30s timeout, deterministic non-fabricated fallback, JSON-object response contract) | ✅ Done — `chatops_answerer.py` + `chatops_context.py`, `services/explainability-service/tests/test_chatops_answerer.py` |
| `api-gateway`: `POST /{project_id}/ask` — thin proxy, `tenant_id` resolved from the authenticated session (never the request body), `_load_project` 404s a cross-tenant project id | ✅ Done — `projects_router.py`, `services/api-gateway/tests/test_project_chatops.py` |
| Frontend: `ChatOpsPanel` on the Pipeline View tab | ✅ Done — `frontend/src/components/pipeline/ChatOpsPanel.tsx`, `frontend/src/api/chatops.ts` |
| Read-only by construction — assembles context from already-computed verdicts/audit rows only, never touches pipeline/verification/actuation code, cannot influence a real rollout decision | ✅ By design |

**Also fixed same session:** a pipeline run rejected by the per-tenant concurrency lock (`worker.py`) used to vanish — no `execution_state` row was ever written, so it stayed `PENDING` in the UI forever with nothing for the crash-recovery reconciler to find. It now writes a real terminal `FAILED` execution state with an explicit reason the moment the lock is denied. Covered by `services/pipeline-worker/tests/test_tenant_lock_rejection_writes_terminal_state.py`.

### 9.8 — Historical Pipeline View: stage timeline + log replay (2026-09-18)

Found live: opening a run older than 24h (both `state:{run_id}` and `logs:{run_id}`'s Redis keys carry a 24h TTL) showed "Waiting for pipeline DAG…" and "Waiting for log output…" forever — indistinguishable from a run that genuinely never started. Root cause was two separate gaps, both now fixed and live-verified against a real triggered run (build → test → canary_deploy → progressive_verify, HEALTHY verdict) with its Redis keys deliberately deleted to simulate expiry:

| Item | Status |
|---|---|
| Stage-DAG fallback — `execution_state` never persisted `stages` (only a single `current_stage` string), so `GET /pipelines/runs/{id}`'s Postgres fallback had nothing to return once Redis expired | ✅ Fixed — falls back to `PROJECT_STAGES`, the same declared-stage-list fallback `projects_router.py`'s own `/{project_id}/runs/{run_id}/stages` endpoint already used |
| `stage_logs` (`db/schema.sql`, real schema + RLS since Phase 8) had never actually been written to by anything | ✅ Fixed — `worker.py`'s `_log()` now persists every line (tenant-scoped, per-stage) via a new `PipelineWorkerDB.save_stage_log`; verified 14 real rows for one full run, correctly attributed per stage |
| `logs/stream` SSE endpoint had no replay source once Redis's list expired, and would poll an eternally-empty key forever for an already-terminal run | ✅ Fixed — falls back to `stage_logs`, ordered, then closes the stream (nothing further can ever arrive for a terminal run) instead of polling forever. **Real bug hit and fixed while verifying this**: the historical query must run *before* `EventSourceResponse` is constructed, not inside the generator — `db` is the request-scoped session, and the generator runs after the route handler returns, by which point its transaction is already closed (`InvalidRequestError: Can't operate on closed transaction`, confirmed live) |

Full pipeline-worker (196 passed, 5 skipped) and api-gateway (148 passed) suites green — no regressions.

---

### 9.9 — AI infra: existing-vs-AI-created + prompt-driven editing (2026-09-26/27)

Design and research in `AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md`. All of it live-verified (real AWS, real Groq, real browser).

| Item | Status |
|---|---|
| "Existing vs AI-created" source choice; read-only, archetype-scoped discovery of real AWS resources (`shared/provisioning/aws_discovery.py`); every pick re-verified against AWS server-side | ✅ Live-verified |
| Real import: `ImportExistingResources` change set showed `Import DBInstance`; the import **executed** (`CREATE_COMPLETE`); deleting the importing stack **left the DB running** (`DeletionPolicy: Retain`) | ✅ Live-verified against a throwaway RDS instance (deleted afterwards; account confirmed clean) |
| `DeletionPolicy: Retain` on imports and "an edit can never remove/un-retain one" enforced in code (`infra_generator.py` validators), not just prompted | ✅ Live-verified — an edit told to remove the DB was rejected, corrected on retry, DB still retained |
| Prompt-driven edit (`POST /infra-drafts/{id}/edit`): new linked draft (`parent_draft_id`, migration `0018`), re-enters the approval gate, inherits the parent's stack so the change set is an UPDATE of the same stack | ✅ Live-verified |
| Wizard UI: source toggle, discovery picklists, "Edit with AI", "Revised" badge | ✅ Verified in Chromium (topology 4 → 5 nodes with the cache added) |
| Live-only bugs found and fixed: edit ignored because the base prompt forbade off-archetype resources; unchanged edit now rejected; hard 413 (edit request > the whole 8,000 TPM cap) fixed by slimming the prompt; `json_validate_failed` 400 retried once; multi-level over-escaped template JSON repaired; 150s gateway budget; empty timeout message | ✅ Fixed + unit-tested. Rejected approaches (don't retry): lowering edit `max_tokens` truncates larger edits; `reasoning_effort=low` breaks strict JSON |

Not run: an execute of the agent-generated FULL stack (ALB + ECS + DB) — deliberately, it creates billable resources; only its change-set preview ran.

Tests: api-gateway 265, pipeline-worker 214 (5 skipped), explainability `test_infra_generator.py` 32 — all green.

### 9.10 - Infra agent hardening: independent cost, policy, BYO-AWS, failure RCA, cutover UI, right-sizing (2026-09-26)

| Item | Status |
|---|---|
| **Independent infra policy** - `policies/infra_guardrails.rego` (`infra.guardrails`) evaluates the REAL CloudFormation template (encryption, public access, Retain on imports, budget); api-gateway blocks approval (409) on a deny; fail-closed if OPA is down | Live-verified; OPA 39/39. A model that omitted `StorageEncrypted` was blocked, then the generator prompt was fixed |
| **Independent cost** - `shared/provisioning/aws_pricing.py` prices the template from the AWS Price List API, never trusting the model's number; falls back to a labelled `ai_estimate_unverified` | Live-verified end to end: a real draft priced $46.72/month from the AWS Price List vs the model's own $43.00 (both kept, labelled separately); OPA allowed it. Also verified across regions (found and fixed Multi-AZ / ElastiCache / regional-prefix selector bugs) |
| **Cross-account (BYO-AWS)** - `aws_connections` table (migration `0019`), platform-generated ExternalId, customer-run CloudFormation role, verified by a real `sts:AssumeRole`; every provisioning call takes the connection | Live-verified end to end: real role, confused-deputy denial, least-privilege denials, and a real draft whose change set (9 Adds) was created through the assumed role. Live-only bug found and fixed: a draft row returns `aws_connection_id` as `uuid.UUID`, which the loader rejected as 'not found'. UI built (`AwsAccountPicker`), not yet browser-verified. Test stacks and rows deleted |
| **Failure RCA** - `POST /infra-drafts/{id}/failure-analysis`: real CloudFormation events (`fetch_failure_events`) -> deterministic root-cause selection -> Groq explanation whose evidence is verified verbatim against the events, with a rule-based fallback; UI card can hand the fix to "Edit with AI" | Live-verified on a real failing stack (S3 name conflict) - real cause retrieved and correctly explained. `pipeline-worker` now pins boto3 1.43.103 (CloudFormation `DescribeEvents`, where early-validation failures put their real reason) |
| **Blue-green cutover panel** (`BlueGreenCutoverPanel.tsx`, log-derived phases) | Unit-tested (12); not yet browser-verified |
| **CloudWatch right-sizing** (`compute_ecs_rightsizing`, Fargate size snapping, baseline-only) | Live-verified on a temporary Fargate service (torn down) |

Traps found: api-gateway has no boto3 (shared modules it imports must stay boto3-free - `aws_arn.py`); `aws_connection_id` FK is `ON DELETE RESTRICT`; the ExternalId travels only in request bodies, never URLs; Groq's daily token quota and `Retry-After` parsing (`56m57s` is not 57s).

Tests: gateway 340, pipeline-worker 280 (5 skipped), explainability 91, policy-controller 132, OPA 39, vitest 36 - all green.

### 9.11 - Live verification of blue-green on real AWS ECS (2026-09-26)

Public `traefik/whoami` images (answer on any path, so the ALB path-prefix trap does not apply) deployed by the platform itself into the `smartcd-platform` cluster; everything torn down afterwards (no ALB, cluster, tasks or ENIs left).

| Item | Status |
|---|---|
| Onboarding creates cluster, shared ALB, target groups, listener rule, baseline+canary Fargate services; live URL served by the baseline | Live-verified |
| Blue-green rollout: stabilize, ALB health gate, atomic cutover (blue 100/0 -> 0/100), live-URL verification, graduation, `COMPLETED` (~140s) | Live-verified |
| Blue-green cutover panel in a real browser against that run: phases advanced stabilize -> health -> switch -> verify -> graduate and the blue/green traffic split flipped 100/0 -> 0/100 -> 100/0 | Browser-verified |
| Project deletion deprovisions services, target groups and listener rules | Live-verified (`succeeded: True` after the timeout fix) |
| Post-cutover automatic rollback | NOT live-verified - unit-tested only (see BACKLOG P0 row 2) |

Live-only bugs found and fixed: (1) a pipeline generated with `manual_approval_required=false` / `auto_graduate` had `minDuration: 0s` on its 100% step, which the worker's own schema rejects, so every such run failed validation (generator now gives an automated final step a 120s floor and the guardrail sample size; regression tests). (2) api-gateway called pipeline-worker to onboard/deprovision with 60s/30s timeouts, reporting "unreachable" (empty message) for work that then completed - now 300s with a named exception. (3) `AWS_ALB_BASE_URL` defaulted to a hardcoded, long-dead ALB hostname (compose default now empty; still needs setting per ALB - BACKLOG P2 7a).

Browser verification of the earlier infra UI (11 checks, real backend and Groq): AWS account picker (connect flow, real rejected AssumeRole), draft cost/policy labels, failure-analysis card and its "Use as an edit instruction" hand-off to Edit with AI. Template-format errors are now classified as `invalid_configuration` with an edit hint.

### 9.12 - Repo Health analysis rewritten (2026-09-26)

The build-step card ("Repo Health") used to headline an IsolationForest anomaly score trained on five sample repos, so a Dockerfile repo with a missing LICENSE could show "HIGH RISK - 0% readiness", every finding was a flat one-liner, and it printed hosting prices. Now: `shared/repo_report.py::assess_readiness` computes a deterministic, weighted 0-100 readiness score over deployment-relevant checks (build method 25, tests 20, lockfile 20, CI 10, README 10, license 5, declared deps 5; checks that do not apply, such as a lockfile for a static site, are skipped). Each finding has a severity (critical / important / minor), a problem statement, why it matters and a concrete fix; passed checks are listed too. Nuance: a known language with no start command is "important" (a Dockerfile can be generated), only an unidentifiable build is "critical"; test files with no test command earn half credit and their own finding (this removed a contradiction with the wizard's detection line). Level: high only for a critical finding or a score under 40. The anomaly score is still returned as `anomaly_score` but no longer drives anything. The summary sentence is built from the findings, names the single most valuable fix, and never mentions cost (pricing appears only after the infrastructure proposal is generated). 13 new tests; verified in Chromium against a real repo.

### 9.13 - Infra agent decides WHAT to build (2026-09-26)

Problem: for a bare Express app (`test_zyx`: no database/cache/storage) the AI produced a 9-resource stack - its own ALB, listener, target group, cluster, service, task definition and two security groups - because it was asked for a self-contained template and did not know the platform already builds all of that. That cost ~$22/month more for a second ALB, differed from the platform's real topology (no baseline/canary), and needed far more tokens (more Groq limit and malformed-JSON failures).

Now (`shared/infra_needs.py`, deterministic, no AI): `analyze_infra_needs` decides from the human-locked IntentSpec flags (pre-filled from real repo detection) what is needed *beyond* what the platform already provides. Nothing extra -> `build_standard_only_proposal`: no AI call, no template, no change set, $0, and the UI shows a "Nothing extra to build" card listing what the platform provides. Extras needed (database, cache, object storage, worker service) -> the gateway reads the platform's real VPC/CIDR/subnets (`POST /infra-provisioning/platform-context`, `get_platform_network_context`) and sends only the extras plus that network to the AI in "extras-only mode"; `_require_only_additions` rejects (with a corrective retry) any template that recreates the ALB, listener, target group, cluster or, for data extras, a service/task definition. Editing a no-extras draft returns a 409 that says how to add something; there is no change set for it.

Live-verified on the real repo in Chromium: no extras -> card, no AI call, approvable; database on -> real Groq produced 3 resources (SecurityGroup, DBSubnetGroup, DBInstance) in the real VPC, $15.44/mo from AWS prices (was $31-47), OPA allowed. Also fixed: repo detection failed for PUBLIC repos whenever a stale stored GitHub token existed (the anonymous fallback only caught 400/401, but a rejected token deliberately maps to 502).

Design choice: the human's flags win - toggling "Needs a database" off removes it even if the archetype suggests one. Not verified: the cache/storage/worker extras against real Groq (unit-tested only).

### 9.14 - "Guaranteed Live Web App CI/CD" P0/P2 batch closed out (2026-09-27)

All of BACKLOG's P0 rows (2, 4, 5, 6) and P2 rows (7, 7a, 7b) - live-verified or fixed against the real AWS account, all test resources torn down afterward, nothing left billing.

| Item | What was done |
|---|---|
| #2 - post-cutover automatic rollback | Live-verified with a custom throwaway nginx image (`/healthz` -> 200, everything else -> 500, trailing-slash-agnostic - httpbin's `/status/500` route turned out to 404 on the trailing slash `build_live_url` always adds, so a purpose-built image was used instead). Real blue-green cutover to 100% canary, `verify_live_url` correctly saw the real HTTP 500, `rollback_blue_green_ecs_weights` fired, traffic reverted to 100/0 baseline, and the run recorded a real terminal `FAILED` in `execution_state` (not a silent `COMPLETED`) |
| #4 - Dockerfile synthesis | Live-verified for `spa` (a real public Vite/React repo, tldraw/vite-template, with no Dockerfile - built via the two-stage node-build-then-nginx template, real `docker build`, real ECR push) and `static` (AfriDeploy/template-static-html, the plain nginx template) - both served real 200s through the ALB. `nextjs` not verified this round (hit GitHub's unauthenticated rate limit hunting for a suitable small standalone repo - not a platform issue, just not exercised) |
| #5 - health-check defaults + ALB matcher | Confirmed already correctly built (`shared/repo_scanner.py::suggest_networking_defaults` - static/spa suggest port 80 + `/`, nextjs suggests port 3000 + `/`) and now live-verified: real detection against both repos returned the right values, and the resulting target groups showed `HealthCheckPath=/`, `HealthCheckPort=traffic-port`, `Matcher: 200-399`, both targets healthy |
| #6 - live-URL verification semantics | Decision: keep "<500 = verified" (the platform can't know what the RIGHT page looks like for an arbitrary app), but close the one mechanically detectable gap - the ALB's own default listener action returns a fixed, platform-authored 404 body ("No service registered at this path.") when no project's rule matched at all; that exact response now never counts as verified, closing the part of the ALB-path-prefix-trap bug class that was silently passing through as "<500" |
| #7a - `AWS_ALB_BASE_URL` goes stale | `shared/provisioning/alb_resolver.py` resolves the shared ALB's DNS name live from AWS by its stable NAME, with a 120s in-process cache invalidated the instant the ALB is (re)created; pipeline-worker/policy-controller call it directly, and publish the result to Redis (`platform:alb_dns_name:{region}`) so api-gateway's display-only `_live_url` (no boto3) reads it too. The static env var is now only a last-resort fallback |
| #7b - failed-validation pipelines retried forever | A manifest that fails `load_pipeline`'s schema validation (deterministic - retrying can never produce a different outcome) is now acked and recorded as a real terminal `FAILED` run on the FIRST attempt, mirroring the existing tenant-lock-rejection fix, instead of being silently re-delivered and re-validated against the same invalid YAML for `MAX_DELIVERY_ATTEMPTS` cycles |
| #7 - orphaned ALB listener rule on a path-prefix change | `ensure_listener_rule`'s existing-rule lookup now matches by this project's stable target-group ARNs (never by the current path condition), so a changed path_prefix updates the SAME rule (condition and actions both) instead of creating a second, orphaned one |

Live-only bug found and fixed along the way: `_live_url` becoming async (to read the published Redis key) needed threading `request` through `_load_project`'s 12 call sites - a request-scoped shared helper, not obvious from any single call site.

Tests: gateway 375, pipeline-worker 297 (5 skipped), policy-controller 122 (+10 needing a live OPA server, unaffected), explainability 108 - all green. AWS swept clean across all 17 regions afterward (no clusters/ALBs/ECR repos/IAM roles/security groups left).

---

## Known open bugs / small gaps (not full sub-phases, but real and undone)

| Gap | Where |
|---|---|
| `_apply_http_route`'s 409-conflict branch only logs, doesn't patch — not self-healing | `actuation_executor.py` |
| Orphaned ALB listener rule if a project's `path_prefix` changes between onboardings | AWS ECS path (low severity — `path_prefix` isn't user-editable post-creation today) |
| `eksctl delete cluster` alone leaves an orphaned classic ELB + security group | EKS teardown — must delete the ELB/SG first |
| Root AWS account password rotation (pasted in chat by mistake earlier session) | Operational — user's own action |

---

## Decided next steps (2026-09-16) — pick one to build

Given the stated priority — real git→cloud CI/CD, a genuinely live URL,
reports/cost as the main payoff — these three, in this order, are what's
actually left to make that literally true on the ECS target. None are new
designs; each extends a mechanism that already works.

| # | Task | What it takes | Status |
|---|---|---|---|
| 1 | **ECS Fargate cost tracking** | Port `cost_tracker.py`'s exact formula — real resource footprint × a configurable `$/vCPU-hr`+`$/GB-hr` rate — to read ECS task definitions instead of K8s Deployments | ❌ Not started |
| 2 | **GitHub webhook receiver** (the actual "git push → cloud" trigger) | New endpoint, fast-ack + async via the existing Redis Streams pattern, dedupe on `X-GitHub-Delivery`, calls the same `POST /{project_id}/rollout` a human trigger already calls | ❌ Not started |
| 3 | **Reports & Cost UI tabs** | Two new frontend tabs reading the already-real `GET /reports/digest/{tenant_id}` + `GET /reports/deployment/{run_id}` endpoints, plus the new ECS cost numbers from #1 | ❌ Not started |

Recommended order: #1 unblocks reports being *complete* for the ECS
target → #2 makes it truly autonomous, not human-triggered → #3 makes all
of it visible to the user. Say which one (by number or name) to start.

---

## Housekeeping

All of Phase 9.2/9.3/9.5(ECS)'s work is currently **uncommitted** in the
working tree as of this writing (see `git status`) — confirm what's staged
before assuming any of the "Done" items above are on `main`. The EKS work
(9.5) **is** committed (`6783493`, `4169158`).
