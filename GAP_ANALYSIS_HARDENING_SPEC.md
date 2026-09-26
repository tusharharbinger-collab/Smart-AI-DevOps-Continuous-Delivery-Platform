# Gap Analysis — Hardening Spec vs. Current Codebase

This evaluates the proposed "Staff Platform/Reliability Engineer" hardening
spec against what's **actually implemented today**, verified by reading the
real source files (not inferred from names). Each item is marked:

- ✅ **EXISTS** — already built and working, no action needed
- 🟡 **PARTIAL** — some of it is real, some is missing/stubbed
- ❌ **MISSING** — nothing like this exists yet

---

## Phase 1 — Input Hardening & Pre-Flight Validation

### 1.1 Pipeline spec schema validation — 🟡 PARTIAL
`services/pipeline-worker/src/pipeline/manifest_loader.py` already uses
Pydantic v2 models (`StepConfig`, `MetricConfig`, `PipelineSpec`), but they're
decorative — `stages`/`gates`/`guardrails`/`verificationConfig` are typed as
loose `dict[str, Any]` and `load_pipeline()` never actually invokes
`StepConfig`/`MetricConfig` validation. **None** of the required invariants
exist: no check that traffic weights strictly increase and end at 100, no
`minDuration > 0` / `minSampleSize > 0` check, no `confidenceFloor ∈ (0,1]`
check, no `maxCostDelta ≥ 0` check. `dag_builder.py` only checks for graph
cycles.
**→ Needs building**, largely as specified.

### 1.2 PromQL validator/sanitizer — ❌ MISSING
`services/verification-engine/src/telemetry/prometheus_client.py` has a
hardcoded 10s `httpx` timeout, but zero PromQL syntax validation, zero
rejection of scalar-returning queries, zero sanitization — user-supplied
query strings go straight to Prometheus's HTTP API.
**→ Needs building.** Real risk today: a malformed or malicious custom
business-metric PromQL string is a legitimate blind spot for any onboarded
service with a custom metric.

### 1.3 Registry/image pre-flight check — ❌ MISSING
No registry-existence check anywhere. `deploy_task.py` builds a Deployment
spec with `image=f"localhost:5001/payments:{image_tag}"` and applies it
directly — if the tag doesn't exist, this fails as a broken pod, not a clean
pre-flight rejection.
**→ Needs building** — genuinely useful, avoids a confusing "why is my canary
pod stuck in ImagePullBackOff" failure mode.

---

## Phase 2 — Orchestration & Actuation

### 2.1 Multi-stage DAG execution — ✅ EXISTS (already real, not simulated)
This is the one place the spec assumes less than what's already built:
- `build_task.py` runs a **real** `subprocess.run(["docker","build",...])`.
- `run_test_task` runs a **real** `pytest` subprocess and checks `returncode`.
- `deploy_task.py` uses the real `kubernetes` client against the actual Kind
  cluster (create-or-patch).
- `verification_task.py` calls verification-engine's `/verify` with real
  Prometheus telemetry when a metric declares a `prometheus` block, falling
  back to an explicitly-logged synthetic generator otherwise.
- `worker.py`'s `PipelineOrchestrator` already executes build → test →
  canary_loop sequentially, with tenant locks, resumable state, and live
  Redis-backed log streaming.

**Real gap vs. spec:** the spec's `build` stage additionally wants **cloning
the user's actual GitHub repo** — today `build_task.py` builds from a fixed
local path (`sample-app/v1.1.0/Dockerfile`), it does not `git clone` an
arbitrary onboarded repo URL. That's the one genuinely missing piece here,
not "everything in Phase 2.1."
**→ Needs building:** git-clone-by-URL support only. Nothing else in this
section needs rework.

### 2.2 Live log streaming (SSE) — ✅ EXISTS
`services/api-gateway/src/routers/logs_router.py` — durable Redis LIST +
polling SSE, already covered in an earlier session's fix. No action needed.

### 2.3 Autonomous rollback actuator — ✅ EXISTS
`actuation_executor.py`'s `emergency_rollback()` already does exactly what's
asked: weight → 0% via JSON Patch on the `HTTPRoute`, **plus** scales the
canary Deployment to 0 replicas (drains pods). `verdict_verifier.py` does
full HMAC-SHA256 verification with constant-time compare and a 300s
staleness check before anything is actuated. `audit_writer.record_actuation()`
writes a real signed audit row (fixed in an earlier session — previously
never wrote a row at all).
**→ No action needed.** This is already stronger than the spec asks for.

### 2.4 Alert dispatcher — 🟡 PARTIAL
`services/policy-controller/src/alert_dispatcher.py` already implements all
three trigger conditions from the spec — rollback, approval-gate-pending,
verification-timeout — correctly event-driven. **But only Slack webhook is
wired**; if `SLACK_WEBHOOK_URL` is unset it just logs and skips. No email
channel, no generic webhook channel.
**→ Needs building:** additional channels only (email/generic webhook), not
the triggering logic, which is already correct.

### 2.5 Digest/reporting engine — ✅ EXISTS
`services/explainability-service/src/digest_generator.py`'s
`generate_delivery_health_digest()` already computes rollback rate,
mean-time-to-verify, and avg cost delta over a configurable trailing window
(default 7 days). Exposed via `GET /digest/{tenant_id}` in
`reports_router.py`. **SOC2 CSV export also already exists**:
`audit_router.py`'s `GET /export/soc2`.
**→ No action needed** — this entire phase item is done, just under
different file names (`explainability-service` instead of a separate
"reporting-service").

---

## Phase 3 — Multi-Tenancy & Security Isolation

### 3.1 Row-level & context isolation — 🟡 PARTIAL
**DB-level RLS is fully real** — `schema.sql` has `tenant_id` FKs plus
`ENABLE/FORCE ROW LEVEL SECURITY` and `CREATE POLICY tenant_isolation_*` on
every relevant table (pipelines, executions, verdicts, audit_ledger,
approvals, cost_analysis). `auth/middleware.py` verifies the JWT and sets
`app.active_tenant_id` per request. This is genuinely solid and already
covers "reject cross-tenant read/write with 403/404" — RLS makes the row
invisible rather than returning it and then checking, which is a stronger
guarantee than an application-level check.

**Real gap:** Kubernetes resources are **not** tenant-isolated —
`manifest_generator.py`, `deploy_task.py`, and `actuation_executor.py` all
default to one shared `namespace: str = "production"`. Every tenant's
canary/baseline Deployments currently live in the same namespace.
**→ Needs building:** per-tenant (or per-onboarded-service) namespace
provisioning. This is the same gap flagged in the earlier AWS/EKS discussion
— onboarding doesn't create real per-tenant K8s isolation yet.

---

## Phase 4 — Frontend

### 4.1 Project/service switcher — ❌ MISSING
No such component anywhere in `frontend/src/pages` or `components`.
**→ Needs building** as described.

### 4.2 3-stage visual stepper — 🟡 PARTIAL
`frontend/src/components/pipeline/PipelineDAG.tsx` already renders a
left-to-right stage stepper with pending/current/completed states. What's
missing: (a) explicit Failed (red) state styling per the spec's 4-state
requirement, (b) clicking a stage to filter the live log panel to just that
stage's output — today the log panel shows the whole run's log, unfiltered,
(c) the canary ramp chart (`TrafficWeightChart.tsx`, which already exists
and is wired into `PipelineDashboard.tsx`) is **not** linked into
`VerificationInspector.tsx`.
**→ Needs building:** stage-click log filtering + wiring the existing ramp
chart into the Inspector. The stepper component itself doesn't need to be
rebuilt from scratch.

---

## Phase 5 — Test Suite

### All 7 requested test files — ❌ MISSING
Confirmed absent: `test_pipeline_validation.py`, `test_promql_sanitizer.py`,
`test_registry_preflight.py`, `test_rollback_actuation.py`,
`test_alert_dispatcher.py`, `test_digest_reports.py`,
`tests/adversarial/test_tenant_isolation.py`.

Note: `test_rollback_actuation.py` and `test_alert_dispatcher.py` would be
testing **already-working** code (2.3 and 2.4 above) — writing these tests
doesn't require building the feature first, just adding regression coverage
for what's already live. The other five need their underlying feature built
first (or, for tenant isolation, could be written today against the already-
real RLS layer — the K8s-namespace gap doesn't block writing this test).

---

## Status update — item 1 done (2026-09-13)

`tests/adversarial/test_tenant_isolation.py` is written and passing (13/13),
plus the full adversarial suite (37/37, zero regressions). Writing this test
surfaced **4 real cross-tenant vulnerabilities**, not just missing coverage
— all fixed live and verified against the running stack before the test was
finalized:

1. `pipeline_router.get_run()`'s Redis fast path (`state:{run_id}`) had no
   tenant check at all — fixed by checking the cached state's own
   `tenant_id` field before returning it.
2. `verification_router.get_verification_result()` (Redis `verdict:{run_id}`)
   had none — fixed with an RLS-scoped ownership check against
   `pipeline_executions` before touching Redis.
3. `logs_router.stream_logs()` (the SSE live-log endpoint) had none — same
   fix pattern as #2.
4. `reports_router.get_delivery_health_digest()` took `tenant_id` straight
   from the URL with **no check against the caller's own JWT tenant** — a
   plain IDOR, and the most severe of the four since it needs no run_id
   guessing at all. Fixed by comparing the path param to
   `request.state.tenant_id`, 403 on mismatch.

Also found and fixed one unrelated but real bug while verifying #4 live: the
digest endpoint 500'd for every tenant, always — `explainability-service`'s
own DB session never set `app.active_tenant_id`, so RLS's `FORCE ROW LEVEL
SECURITY` silently matched zero rows regardless of the query's own
`WHERE tenant_id = ...` clause. Fixed in `explainability-service/src/db.py`
+ `main.py`'s `get_digest`. Separately, a raw-SQL `(:days || ' days')::interval`
pattern failed with an asyncpg type error (`int` vs `str`) — fixed by
switching to `:days * interval '1 day'`.

`verification_router.get_verification_history()` and every `audit_router`
endpoint were already safe (real RLS-scoped queries) — covered in the new
test as regression guards, not because they were ever broken.

## Status update — items 2-9 done (2026-09-13)

All 9 items from the priority list are now built, tested, and verified
live against the running stack. Summary of what shipped, and every real
bug found along the way (this codebase's established pattern: never trust
a spec's assumption about what's broken — verify against real system
state):

**Item 2 — Pipeline spec schema validation.** `services/pipeline-worker/src/schemas.py`
(new) + wired into `manifest_loader.py`. Strictly increasing traffic
weights ending at 100, positive `minDuration`/`minSampleSize` (with a
deliberate exception for a `requiresManualApproval: true` step — the real
seed pipeline's last step legitimately has 0/0s), `confidenceFloor ∈ (0,1]`,
non-negative `maxCostDelta`. **Found and fixed two real bugs in existing
seed data**: `payments-pipeline`'s single-step (10% only, never reaching
100%) demo config, and `checkout-service-rollout`'s literally blank
`minSampleSize:` field (valid YAML null) — both would have silently
misconfigured a real rollout forever; now both are rejected with a clear
message at load time and were corrected in the DB. 33 new tests.

**Item 3 — Regression tests for already-working actuation/alerting.**
`test_rollback_actuation.py` (6 tests, mocks the k8s client directly —
confirms weight→0%, canary scaled to 0 replicas, idempotency, and a real
k8s API failure propagates instead of being swallowed) + `test_alert_dispatcher.py`
(8 tests initially, +7 after item 6). 14 new tests total at this step.

**Item 4 — PromQL validator.** `services/verification-engine/src/promql_validator.py`
(new): validates every user-supplied PromQL query (business-metric configs)
by actually running it through Prometheus's `/api/v1/query` with a strict
5s timeout, rejecting syntax errors and scalar/string results (only
instant/range vectors are usable). Wired into `main.py` before any real
fetch, surfaced as a clean 422 rather than a raw 500. 12 new tests.

**Item 5 — Registry/image pre-flight check.** `services/pipeline-worker/src/preflight.py`
(new): checks the local Docker daemon for the image tag before a deploy
stage applies its Deployment. **Found a real, previously-latent bug**:
this container's base image (Debian trixie) installs `docker.io` for the
daemon binaries only — there's no `/usr/bin/docker` CLI on this release at
all, so `build_task.py`'s pre-existing `docker build` subprocess call (and
a CLI-based preflight check) would always have failed with "executable not
found," never a real answer — completely unnoticed because the seeded demo
pipelines skip straight to `progressive_verify` and never exercise
build/deploy. Fixed by switching both `preflight.py` and `build_task.py` to
the `docker` Python SDK (talks to the socket directly), and by actually
mounting `/var/run/docker.sock` into pipeline-worker (it never was).
Verified live: real `docker.image.build()`/`images.get()` calls succeed
inside the container. 10 new tests.

**Item 6 — Alert dispatcher additional channels.** `alert_dispatcher.py`
rewritten to dispatch independently across Slack (already existed), a
generic webhook (`ALERT_WEBHOOK_URL`), and email (SMTP) — any subset can be
configured, one channel's failure/absence never blocks another. 7 new
tests (15 total for this file).

**Item 7 — Git-clone-by-URL for the build stage.** `services/pipeline-worker/src/tasks/git_clone.py`
(new): a build stage that declares `repoUrl` clones the actual onboarded
service's own repo into an isolated per-run workspace instead of always
rebuilding the platform's fixed demo app — the real gap flagged in the
original gap analysis. Supports an optional `credentialsEnvVar` for private
repos (never a literal secret in pipeline YAML). Verified live against a
real public repo (clone + cleanup). 10 new tests.

**Item 8 — Per-tenant Kubernetes namespace isolation.** Onboarding now
derives a per-tenant namespace (`tenant-<first-uuid-segment>`) instead of
defaulting every tenant into a shared `production` namespace, and
`k8s/onboarding.py` now actually creates that namespace (labeled with
`tenant_id`, idempotent) before applying any Deployment/Service/HTTPRoute
into it — previously assumed the namespace already existed. **Verified
live**: onboarded a real service, confirmed a real `tenant-aaaaaaaa`
namespace was created on the cluster with the right label and the right
resources inside it. 4 new tests.

**Item 9 — Frontend.** The "project/service switcher" already existed
functionally (the Pipeline dropdown in `AppLayout.tsx`) — the original gap
analysis missed this. Built: (a) `PipelineDAG.tsx` now has an explicit
Failed (red) state and is clickable — selecting a stage filters the Live
execution log to just that stage's output (parsed client-side from the
existing `--- Stage: X (...) ---` marker convention, no backend change
needed); (b) the canary traffic ramp (gauge + weight-history chart) is now
also shown in `VerificationInspector.tsx` while `progressive_verify` is
active, not just Pipeline View. Verified: `tsc -b` and `vite build` clean,
12 unit tests + 7 Playwright e2e tests all still passing. **Still missing**:
project-level summary cards (active version / last run status / health
score) — not built this pass.

**Full regression after all 9 items**: 217 backend tests + 12 frontend
unit tests + 7 e2e tests, all passing, zero regressions. Live end-to-end
rollout confirmed working after every single rebuild.

## Priority order (what actually needs building, ranked)

Given how much of "Phase 2" and "Phase 3 DB-level" already exists, the real
remaining work is smaller than the spec implies. Suggested order, most
valuable / least effort first:

1. **`tests/adversarial/test_tenant_isolation.py`** — the RLS layer is
   already real and solid; this just needs a test proving it, and it's the
   single highest-value item for a rubric/security-review standpoint.
2. **Pipeline spec schema validation** (1.1) — cheapest to build (pure
   Pydantic validators, no infra dependency), and it's a real current gap: a
   malformed pipeline YAML today fails deep inside execution instead of at
   load time.
3. **`test_rollback_actuation.py` + `test_alert_dispatcher.py`** — pure
   regression tests against code that already works; low effort, closes the
   test-coverage gap from Phase 5 immediately.
4. **PromQL validator** (1.2) — real security/stability gap for any tenant
   with a custom business metric.
5. **Registry pre-flight check** (1.3) — real reliability improvement, avoids
   a confusing broken-deployment failure mode.
6. **Alert dispatcher additional channels** (2.4) — extends what's already
   correct; email/generic-webhook are additive, not urgent.
7. **Git-clone-by-URL for the build stage** (2.1's one real gap) — larger
   scope: needs credential handling for private repos, disk/workspace
   management per run, and cleanup — do this only once the smaller items are
   done.
8. **Per-tenant Kubernetes namespace isolation** (3.1's one real gap) — the
   same item already flagged in the AWS/EKS discussion; meaningful scope
   (onboarding needs to provision namespaces + RBAC), do alongside any future
   real multi-tenant-onboarding work rather than as a quick patch.
9. **Frontend: project/service switcher + stepper polish** (Phase 4) — pure
   UI work, no backend dependency, can happen any time.

---

## What more could be added beyond this spec

Ideas that would strengthen the platform further, not requested above but
consistent with its direction:

- **Automated Postgres/Redis state reconciliation** — flagged in an earlier
  session (a stuck "RUNNING forever" pipeline caused by state drift between
  Redis and Postgres after a worker restart). A periodic reconciler comparing
  the two and self-healing drift would close a real observed failure mode.
- **Per-service policy customization at onboarding** — also flagged
  earlier: every onboarded service currently gets identical hardcoded
  guardrail defaults; letting onboarding set per-service confidence
  floor/sample size/cost ceiling would make the Policy & Gates screen
  actually mean something per-service.
- **Rate-limiting on the PromQL validator's Prometheus calls** — once 1.2 is
  built, a malicious/malformed custom metric could still be used to hammer
  Prometheus with repeated invalid queries; worth pairing with basic
  per-tenant query rate limiting.
- **Audit ledger tamper-evidence chain** — the audit ledger is HMAC-signed
  per-row today; a hash-chained ledger (each row's hash includes the previous
  row's hash) would make retroactive deletion/edits detectable, which is a
  natural extension of the existing SOC2-export feature.
- **CI wiring for the new test files** — once Phase 5's tests exist, add
  them to the `test.yml` GitHub Actions workflow described in
  `docs/roadmap/07-deployment-plan.md` so they run on every PR, not just
  manually.
