# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This repo is the in-progress implementation of the **Smart AI DevOps & Continuous Delivery Platform** (a university/assignment build: "Assignment 05"). The entire intended design — architecture, every file's exact contents, rationale for every design decision, and a phase-by-phase build plan — lives in **`MASTER_BUILD_SPEC.md`** at the repo root. That file is the single source of truth for *intended* behavior; treat it the way you'd treat a design doc + rubric combined.

**Beyond the assignment itself, `docs/roadmap/00-ROADMAP.md` is the living plan for turning this into a real product** — frontend overhaul, real telemetry, multi-service onboarding, security hardening, reliability/scale, platform observability, and local→global deployment, each as its own phase file with a checklist. Check it before starting speculative feature work outside the assignment spec's scope.

**Read `MASTER_BUILD_SPEC.md` before making non-trivial changes.** It contains:
- §1: a full rubric-to-file traceability matrix (what must exist and why)
- §3: the complete target repository structure
- §4–§14: exact reference implementations for every component (pipeline orchestration, verification engine statistics, OPA guardrails, explainability, cost analysis, frontend screens, reports/alerts, DB schema, logging, adversarial tests)
- §15: an ordered build execution plan (phases 0–11) with a verification command per phase
- §16: a "Definition of Done" checklist — the acceptance bar for the whole system

**Current state: all 5 backend services + frontend have real source, Dockerfiles, and requirements.txt, and `docker compose up --build` brings up all 10 containers (4 infra + 5 backend + frontend) healthy.** Verified end-to-end live: a verdict produced by verification-engine's `/verify` endpoint is HMAC-signed, published to Redis, picked up in real time by policy-controller's subscriber loop, signature-verified, evaluated against the live OPA server, and correctly blocked/actuated — and RLS-scoped reads through api-gateway correctly isolate tenants over real HTTP requests (confirmed with two different tenant JWTs). The frontend type-checks (`tsc -b`) and builds (`vite build`) cleanly and serves from both `npm run dev` and its Docker image.

**`README.md` and `ARCHITECTURE.md` now exist at the repo root** — read them for the user-facing quick-start and design-decision write-up; don't duplicate their content here.

**The real Kind + Envoy Gateway path is real and has been verified working**, not just configured: `make kind-up && make install-envoy && make deploy-sample-app && make connect-kind-network` (see README) stands up a genuine 3-node cluster with Envoy Gateway, a real `payment-service-baseline`/`payment-service-canary` Deployment pair, and a real `HTTPRoute`. `connect_kind_network.sh` attaches `pipeline-worker`/`policy-controller` to the `kind` Docker network and swaps their kubeconfig's server address from `127.0.0.1:<port>` (host-only, unreachable from another container) to the control-plane container's name. Verified live: an autonomous `HEALTHY` verdict shifted real `HTTPRoute` traffic (confirmed by measuring the actual proportion of canary responses over real HTTP requests), and an autonomous `FAILED` verdict (a real SPRT rejection on an injected error rate) triggered a real rollback — weight to 0%, canary `Deployment` scaled to 0, pods actually terminated — zero manual steps, zero baseline restarts. This exercise caught two bugs invisible to the docker-compose-only demo path, both now fixed: (1) `actuation_executor.py`'s `patch_namespaced_custom_object(..., _content_type=...)` doesn't work at all in `kubernetes==29.0.0` — that kwarg isn't accepted by this client version's generated binding, which hardcodes `application/merge-patch+json`; fixed by calling `ApiClient.call_api(...)` directly to preserve true RFC 6902 JSON Patch semantics. (2) `VERDICT_SIGNING_KEY` can drift out of sync across containers if one service isn't restarted after `.env` changes (docker-compose only re-reads `.env` at container creation) — causes silent verdict-signature-mismatch rejections with no other symptom; if verdicts start getting rejected as forged for no obvious reason, check `docker compose exec <svc> env | grep VERDICT_SIGNING_KEY` across all of verification-engine/policy-controller before suspecting anything else.

Still genuinely missing relative to the full spec: `tests/e2e/*.py` and `scripts/demo/*.sh` (Phase 10) — `scripts/setup/connect_kind_network.sh` now exists but there's no scripted "trigger a healthy rollout" / "trigger a failing one" demo yet. The spec's tech stack names Celery for `pipeline-worker`; the actual implementation still runs stages as plain synchronous function calls (no Celery app exists), but the trigger/verdict mechanism between services is no longer bare Redis pub/sub — Phase 5 (done, see `docs/roadmap/05-reliability-scale.md`) replaced it with Redis Streams + consumer groups (`shared/redis_streams.py`), so running 2+ replicas of `pipeline-worker`/`policy-controller` no longer double-processes every message; `reconciler.py` is now real (pipeline-worker has an actual Postgres connection via `src/db.py`, verified by a real crash-recovery test) rather than dead code. MinIO is up but nothing in the app writes to it yet. RCA generation was switched from Gemini to Groq (`services/explainability-service/src/report_generator.py`, an OpenAI-compatible `/chat/completions` call via `httpx`, no SDK dependency) and is only verified via its deterministic fallback path unless `GROQ_API_KEY` is configured.

**Auth is real and hardened** (Phase 4, done — see `docs/roadmap/04-security-hardening.md`): bcrypt-hashed passwords, HS256-signed 15-minute access tokens actually verified, a 7-day opaque refresh token held in Redis as a revocable allow-list (rotated on every use, revoked on logout), Redis-backed login rate-limiting (5 failures → 429 lockout), a queryable `auth_events` audit trail, and role is now actually *checked* (`src/auth/rbac.py`'s `require_role()`) on pause/resume/rollback, policy saves, and service onboarding — not just carried in the JWT unchecked. Still deferred to Phase 7 (deployment-target-dependent): TLS termination and migrating off a plaintext `.env` to a real secrets store.

**Telemetry is real** (Phase 2 of the roadmap, done) — Prometheus scrapes real `sample-app-baseline`/`sample-app-canary` containers, and `verification-engine`'s `telemetry/prometheus_client.py` feeds genuine PromQL data into the unmodified statistical tests for any pipeline whose metrics declare a `prometheus` query block. The old synthetic generator (`pipeline-worker`'s `_synthesize_telemetry`) is now an explicit, logged fallback for pipelines not yet wired to a metrics source.

When picking up work, check which phase of §15 is next and consult §3 for exactly where new files belong — don't invent alternate structure.

**Two non-obvious traps already fixed once — don't reintroduce them:**
- RLS policies must be declared without `AS RESTRICTIVE` (plain `FOR ALL USING (...)`, i.e. PERMISSIVE). A RESTRICTIVE-only policy with no companion PERMISSIVE policy makes Postgres deny every row to everyone. Similarly, app services must connect via `APP_POSTGRES_DSN` (`app_user`, non-superuser) — the `platform` DSN is a superuser and unconditionally bypasses RLS regardless of `FORCE ROW LEVEL SECURITY`.
- `SET LOCAL` does not accept a bind parameter (`SET LOCAL x = :val` is a Postgres syntax error at execution time) — use `SELECT set_config('x', :val, true)` instead. And route handlers must depend on `get_request_db` (returns `request.state.db`, the session middleware already scoped), never the plain `get_db` — with `NullPool`, a fresh `Depends(get_db)` session is a different physical connection than the one middleware set `app.active_tenant_id` on, so RLS would silently never apply.

## Key design invariants (do not violate these when editing)

1. **No static thresholds.** Every verification decision must come from a statistical test (SPRT, Mann-Whitney, CUSUM, BOCPD, Fisher/Chi-square, Isolation Forest) or a composite score derived from them — never a bare `if metric > X`.
2. **Metric routing is by `category`.** Pipeline YAML metrics declare a `category` (`error_rate`, `latency`, `saturation`, `business_metric`) and `engine.py`'s dispatcher (§6.0) routes purely on that field to the matching test module in `tests_statistical/`.
3. **`verification-engine` is structurally barred from touching Kubernetes.** It has no `kubernetes` dependency and no kubeconfig mount — only `policy-controller` and `pipeline-worker` can actuate cluster changes. Never add a k8s client import to `verification-engine`.
4. **Verdicts are cryptographically signed.** `verification-engine/src/verdict_signer.py` HMAC-SHA256-signs every `ImmutableVerdict` before publishing to Redis; `policy-controller/src/verdict_verifier.py` must verify the signature (and a freshness window) before a verdict is ever handed to OPA. Never let `policy-controller` act on an unverified verdict.
5. **Traffic shifting goes through the HTTPRoute, never the Deployment.** `actuation_executor.py` patches `spec.rules[].backendRefs[].weight` on the Gateway API `HTTPRoute` via JSON Patch — this is what avoids pod restarts. Don't reintroduce replica-count-based traffic shifting for canary weighting.
6. **Multi-tenancy is enforced via Postgres RLS**, keyed on `tenant_id`, set per-request via `SET LOCAL app.active_tenant_id` (see `auth/middleware.py` and `db/schema.sql`). Any new query path must go through a session that sets this context — do not bypass RLS with a superuser connection for app queries.
7. **Sample-size floor is N ≥ 100**, enforced independently in both `confidence.py` (Python) and OPA `minSampleSize` (defense in depth) — don't lower one without the other.

## Commands

Most commands are wrapped in the root `Makefile` (`make help` lists them all). Key ones:

```
make setup            # full bootstrap: infra + Kind cluster + migrations + seed
make up / make down    # docker compose up -d / down
make migrate           # alembic upgrade head (run from services/api-gateway)
make test-all          # test-stats + test-engine + test-guardrails + test-opa
make test-engine       # cd services/verification-engine && pytest tests/ -v
make test-stats        # pytest tests/adversarial/test_statistical_robustness.py -v
make test-guardrails   # pytest tests/adversarial/test_guardrail_bypass.py -v
make test-opa          # opa test policies/ -v
make test-e2e          # pytest tests/e2e/ -v
make demo-healthy / make demo-fail   # scripted rollout demos via scripts/demo/*.sh
make kind-up / make kind-down        # Kind cluster (k8s/kind-config.yaml)
make healthcheck       # curl /healthz across services
```

Running a single test directly (bypassing `make`), e.g. for the verification engine:
```
cd services/verification-engine && python -m pytest tests/test_business_metric.py -v
```
`services/verification-engine/pyproject.toml` sets `pythonpath = ["."]` and `testpaths = ["tests"]`, so tests import via `src.tests_statistical...`, `src.scoring...`, etc. — run pytest from inside that service directory, not the repo root.

`policy-controller` and `pipeline-worker` tests live under their own `tests/` directories (`services/policy-controller/tests/`, `services/pipeline-worker/tests/`) and are plain pytest, not yet wired into a `pyproject.toml`.

Adversarial and e2e tests at the repo root (`tests/adversarial/`, `tests/e2e/`) import across service boundaries (e.g. `from services.policy_controller.src.verdict_verifier import ...`) and are meant to be run from the repo root.

OPA policy tests: `opa test policies/ -v` (requires the `opa` binary — `bin/opa.exe` is vendored on Windows).

## Architecture

Five backend services + a Postgres/Redis/OPA/MinIO infra layer + a Kind-based Kubernetes cluster with Envoy Gateway, wired as a progressive-delivery pipeline:

- **`services/pipeline-worker`** (spec names Celery; actually plain sync function calls + a Redis Streams consumer group, see Phase 5) — parses the declarative pipeline YAML (`pipelines/*.yaml`) into a DAG (`pipeline/dag_builder.py`), executes stages against the real Kind cluster, and persists execution state to both Redis (fast path) and Postgres (durable, resume-after-crash source of truth, via `src/db.py` + `pipeline/execution_state.py`). `pipeline/reconciler.py` runs on worker startup AND on a periodic loop to resume any pipeline left `RUNNING` when a worker died, from the exact stage it was on. Per-(tenant, service) Redis locks prevent concurrent pipelines racing on the same HTTPRoute.
- **`services/verification-engine`** (Python 3.11, isolated) — `engine.py` dispatches each pipeline metric to a statistical test module under `tests_statistical/` based on the metric's `category`. Produces an `ImmutableVerdict` (`verdict.py`), signs it (`verdict_signer.py`), and publishes it over Redis pub/sub (`publisher.py`). This is the only service intentionally denied Kubernetes access (see invariant 3).
- **`services/policy-controller`** — verifies incoming verdict signatures (`verdict_verifier.py`), evaluates them against OPA policy (`opa_evaluator.py` calling into `policies/delivery_guardrails.rego`), and — only on an OPA `allow_action=true` — actuates traffic-weight changes via the Kubernetes Gateway API (`actuation_executor.py`) or dispatches alerts (`alert_dispatcher.py`)/audit records (`audit_writer.py`).
- **`services/api-gateway`** (FastAPI) — the external API surface: pipeline/verification/actuation/policy/reports/audit/health routers, WebSocket + SSE (live logs), Postgres session management with tenant-scoped RLS (`db/session.py`, `auth/middleware.py`).
- **`services/explainability-service`** — builds exact-citation grounded explanations from verdict evidence and calls Groq (`llama-3.3-70b-versatile` by default, via `GROQ_API_KEY`) for root-cause-analysis reports and digests, within a 30s budget with a fallback path.
- **`frontend`** (React + TS + Vite, planned) — four screens: Pipeline View (with SSE log stream), Verification Detail View, Policy & Gates Config, Deployment History & Audit.
- **`policies/delivery_guardrails.rego`** — the single OPA policy gating every actuation: freeze windows, minimum confidence/sample size, manual-approval-required stages, cost-delta ceiling, and right-sizing auto-apply rules. `policies/tests/guardrails_test.rego` covers it.
- **`k8s/`** — Kind cluster config, Envoy Gateway `HTTPRoute`/`GatewayClass` for the payments-service traffic split, and baseline/canary `Deployment` manifests.
- **`shared/logging_config.py`** — structured JSON logging module imported by every service (mounted read-only into each container).

Statistical test responsibilities (all under `services/verification-engine/src/tests_statistical/`):
| category | test | file |
|---|---|---|
| error_rate | Wald SPRT (Bernoulli log-likelihood) | `wald_sprt.py` |
| latency | Mann-Whitney U (+ KS as secondary) | `mann_whitney.py`, `kolmogorov_smirnov.py` |
| saturation | CUSUM + BOCPD (custom NumPy, not `ruptures`) | `cusum.py`, `bocpd.py` |
| business_metric | Fisher's exact / Chi-square (auto-selected by expected cell counts) | `business_metric_test.py` |
| (cross-metric) | Isolation Forest multi-metric anomaly score | `isolation_forest.py` |

`scoring/confidence.py` combines sample sufficiency, variance stability, and elapsed-time stability into a single `C ∈ [0,1]`; `scoring/composite_scorer.py`-equivalent logic weights non-critical metrics into a composite score, while any critical-tier breach or business-metric regression is an immediate hard `FAILED` regardless of the composite.
