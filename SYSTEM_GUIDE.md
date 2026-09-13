# Smart AI DevOps & Continuous Delivery Platform — Complete System Guide

> **Who this is for:** an engineer or evaluator who has just been handed this repository and needs to understand the whole system — what it does, how every piece fits, what is genuinely real, and what is not. It assumes no prior context.
>
> **How this relates to the other docs:** `README.md` is the quick start. `ARCHITECTURE.md` is the short "why we chose this" write-up. `MASTER_BUILD_SPEC.md` is the original design spec. `docs/roadmap/` tracks each phase of work. **This file is the single complete reference** — it explains the system as it actually exists today, including everything the other documents predate.
>
> Last updated after the project-workspaces work (Phase 8) and the retirement of the classic `/app` console.

---

## Table of contents

1. [The one-paragraph thesis](#1-the-one-paragraph-thesis)
2. [Quick start](#2-quick-start)
3. [Life of a rollout — the end-to-end flow](#3-life-of-a-rollout--the-end-to-end-flow)
4. [The five services, and why they are split this way](#4-the-five-services-and-why-they-are-split-this-way)
5. [The statistical verification engine](#5-the-statistical-verification-engine)
6. [The policy layer (OPA)](#6-the-policy-layer-opa)
7. [Data model — every table](#7-data-model--every-table)
8. [Security model — four independent layers](#8-security-model--four-independent-layers)
9. [The user interface](#9-the-user-interface)
10. [GitHub integration](#10-github-integration)
11. [Complete API reference](#11-complete-api-reference)
12. [Configuration — every environment variable](#12-configuration--every-environment-variable)
13. [Testing](#13-testing)
14. [What is real, and what is not](#14-what-is-real-and-what-is-not)
15. [Repository map](#15-repository-map)
16. [Troubleshooting — traps that have bitten before](#16-troubleshooting--traps-that-have-bitten-before)

---

## 1. The one-paragraph thesis

Most CI/CD tooling stops at "the pipeline ran green." Somebody still has to watch a dashboard for twenty minutes after every deploy and make a judgement call. This platform replaces that judgement call with **statistics that a human cannot fudge and a policy engine the deployment cannot bypass**. A new version ships as a canary next to the live baseline; both serve real traffic; genuine hypothesis tests (SPRT, Mann-Whitney U, CUSUM, Fisher's exact) compare them; the resulting verdict is cryptographically signed; and a separate service — the only one with cluster credentials — verifies that signature, asks an OPA policy whether the action is permitted, and only then shifts traffic or rolls back.

The design principle running through the whole system: **no single component can both decide and act.**

---

## 2. Quick start

Requires Docker Desktop.

```bash
cp .env.example .env          # GROQ_API_KEY optional (RCA has a deterministic fallback)
docker compose up -d --build
```

That brings up 16 containers: Postgres, Redis, OPA, MinIO, Prometheus, Loki + Promtail + Grafana, the five backend services, the frontend, and a sample-app baseline/canary pair with a load generator.

| Surface | URL | Credentials |
|---|---|---|
| **Web console** | http://localhost:3000 | `demo@acme-corp.test` / `acme-demo-2026` |
| Second tenant (to see isolation) | same | `demo@other-corp.test` / `other-demo-2026` |
| API + Swagger | http://localhost:8000/docs | Bearer token from `/api/v1/auth/login` |
| Grafana | http://localhost:3001 | `admin` / `admin` |
| Prometheus | http://localhost:9090 | — |

Health check: `docker compose ps` (all should be healthy), then `curl localhost:8000/healthz`.

### Optional: the real Kubernetes target

By default the demo exercises the statistics/policy core without a cluster. To see **real** traffic shifting on a real Gateway API `HTTPRoute`:

```bash
make kind-up              # 3-node Kind cluster
make install-envoy        # Envoy Gateway + Gateway API CRDs
make deploy-sample-app    # images, gateway, canary manifests
make connect-kind-network # wires worker/controller onto the kind network
docker compose restart pipeline-worker policy-controller
```

Verify with `curl localhost:8001/readyz` → `"kubernetes": "ok"`.

---

## 3. Life of a rollout — the end-to-end flow

This is the most important section. Everything else is detail hanging off this spine.

```
  UI: "Trigger New Rollout"
        │
        ▼
  ┌─────────────────┐   INSERT pipeline_executions (status=PENDING)
  │  api-gateway    │   XADD stream:pipeline:start {run_id, policy_yaml, trace_id}
  └─────────────────┘
        │  Redis Stream + consumer group
        │  (exactly one replica picks it up — not pub/sub, which every replica received)
        ▼
  ┌─────────────────┐   parses pipeline YAML → DAG → executes stages in order
  │ pipeline-worker │   build → test → canary_loop
  └─────────────────┘   writes live state to Redis, durable state to Postgres
        │
        │  for each traffic step (10% → 25% → 50% → 100%):
        │  shifts weight, waits for minDuration/minSampleSize, then:
        ▼
  ┌──────────────────────┐  queries Prometheus for real canary vs baseline metrics
  │ verification-engine  │  routes each metric to a test by its declared `category`
  │  (NO kube access)    │  → ImmutableVerdict { status, composite_score, confidence, evidence }
  └──────────────────────┘  → HMAC-SHA256 signs it → publishes to Redis
        │
        ▼
  ┌─────────────────┐  1. verify HMAC signature + freshness window   ← rejects forgeries
  │ policy-controller│  2. ask OPA: allow_action?                     ← rejects policy violations
  │  (HAS kube access)│ 3. write audit_ledger row                    ← before acting, not after
  └─────────────────┘  4. PATCH HTTPRoute weights via JSON Patch     ← no pod restarts
        │
        ├── HEALTHY + confident + policy allows → promote to next step
        ├── FAILED (critical breach)            → roll back to 0% + scale canary to 0
        └── anything else                       → hold for a human
        │
        ▼
  ┌────────────────────────┐  after the decision (never blocking it):
  │ explainability-service │  builds a grounded, citation-backed explanation
  └────────────────────────┘  → rca_summary on the verification record
```

**The critical property:** `verification-engine` has no `kubernetes` dependency and no kubeconfig mounted. It physically cannot act on its own verdict. `policy-controller` has the cluster credentials but cannot produce a verdict. Compromising either one alone is not enough to force a bad deployment.

---

## 4. The five services, and why they are split this way

The split is by **permission**, not by function. Each service holds the minimum authority it needs.

### `services/api-gateway` (port 8000)
The only externally-exposed HTTP surface. FastAPI. Owns authentication, tenant context, and every REST/WebSocket/SSE endpoint the UI calls.

- `auth/middleware.py` — verifies the JWT, then opens a Postgres session and runs `SELECT set_config('app.active_tenant_id', ..., true)` so **every** query in that request is RLS-scoped.
- `auth/rbac.py` — `require_role()` dependency; `developer < lead-sre < platform-admin`.
- `routers/` — pipelines, projects, verification, actuation, policy, reports, audit, logs, services, github.
- `websocket/event_stream.py` — live pipeline state pushes.

### `services/pipeline-worker` (port 8001)
Executes pipelines. Has a kubeconfig.

- `pipeline/manifest_loader.py` — parses and validates pipeline YAML.
- `pipeline/dag_builder.py` — builds a DAG (networkx); a stage with no `dependsOn` implicitly depends on the previous one, so declaration order is execution order.
- `pipeline/execution_state.py` — dual-writes state: Redis (fast/live) + Postgres `execution_state` (durable).
- `pipeline/reconciler.py` — on startup and on a timer, finds runs left `RUNNING` by a dead worker and resumes them **from the exact stage they died on**.
- `tasks/` — `git_clone` (real `git clone` into a per-run workspace), `build_task` (real `docker build` via the Docker SDK), `deploy_task`, `rollout_task`, `verification_task`.
- `k8s/manifest_generator.py` + `k8s/onboarding.py` — generates and applies real Deployments/Services/HTTPRoutes for a newly onboarded service.

### `services/verification-engine` (port 8002) — **structurally sandboxed**
Produces verdicts. **No `kubernetes` dependency, no kubeconfig.** This is enforced by absence, not by convention.

- `engine.py` — dispatches each metric to a test module purely by its declared `category`.
- `tests_statistical/` — the actual maths (see §5).
- `scoring/confidence.py` — combines sample sufficiency, variance stability and elapsed time into `C ∈ [0,1]`.
- `verdict_signer.py` — HMAC-SHA256 over the canonical verdict.
- `telemetry/prometheus_client.py` — real PromQL queries.

### `services/policy-controller` (port 8003)
The only service that mutates the cluster.

- `verdict_verifier.py` — HMAC + freshness check. A verdict that fails this is never handed to OPA.
- `opa_evaluator.py` — calls OPA with the verdict as input.
- `actuation_executor.py` — patches `HTTPRoute` weights via **RFC 6902 JSON Patch**, and scales the canary Deployment to 0 on rollback.
- `audit_writer.py` — writes the signed audit row.

### `services/explainability-service` (port 8004)
Turns a verdict into prose, grounded in its evidence. Calls Groq (`llama-3.3-70b-versatile` by default) with a 30-second budget and a deterministic fallback. Runs **after** the decision — it can never delay or influence a safety action.

### Infrastructure
| Component | Purpose |
|---|---|
| Postgres 16 | All durable state; Row-Level Security for tenancy |
| Redis 7 | Live state, log buffers, Streams (job queues), refresh + GitHub tokens, locks |
| OPA 0.68 | Policy decisions (`policies/delivery_guardrails.rego`) |
| Prometheus | Scrapes the sample-app cohorts; the engine's real metric source |
| Loki + Promtail + Grafana | Log aggregation and the "Platform Health" dashboard |
| MinIO | Running, but **nothing writes to it yet** |
| Kind + Envoy Gateway | The real Kubernetes target |

---

## 5. The statistical verification engine

**Invariant: there is no `if metric > threshold` anywhere in the decision path.** Every decision comes from a hypothesis test.

Routing is by the metric's declared `category` in the pipeline YAML — the dispatcher never hardcodes a metric name:

| `category` | Test | File | Why this test |
|---|---|---|---|
| `error_rate` | **Wald SPRT** (sequential probability ratio) | `wald_sprt.py` | Decides as evidence arrives; can stop early on a catastrophic build without waiting for a fixed sample |
| `latency` | **Mann-Whitney U** (+ Kolmogorov-Smirnov) | `mann_whitney.py`, `kolmogorov_smirnov.py` | Non-parametric — latency is not normally distributed, so a t-test would be invalid |
| `saturation` | **CUSUM + BOCPD** | `cusum.py`, `bocpd.py` | Change-point detection: catches a *drift* that never crosses a static line |
| `business_metric` | **Fisher's exact / χ²** (auto-selected by expected cell counts) | `business_metric_test.py` | Fisher's is exact for small counts where χ² is unreliable |
| cross-metric | **Isolation Forest** | `isolation_forest.py` | Catches multivariate anomalies no single metric shows |

### From tests to a verdict

1. Each metric produces a result with `is_actionable_regression`.
2. **Any critical-tier breach, or any business-metric regression, is an immediate hard `FAILED`** — it is never averaged away.
3. Otherwise non-critical metrics are combined into a weighted composite score (0–100).
4. `confidence` is computed separately from sample sufficiency, variance stability and elapsed observation window.
5. **Sample floor N ≥ 100**, enforced independently in `confidence.py` *and* in the OPA policy — defence in depth, so a bug in one does not open the gate.

Verdict statuses: `HEALTHY`, `DEGRADED`, `FAILED`, `UNVERIFIABLE`.

---

## 6. The policy layer (OPA)

`policies/delivery_guardrails.rego` is the single gate every actuation passes through. It is a separate service, in a separate language, evaluated out-of-process — so a Python bug cannot accidentally authorise an action.

It enforces:
- **Freeze windows** — blocked deploy days/times.
- **Minimum confidence** — below the floor, no autonomous promotion.
- **Minimum sample size** — the N ≥ 100 floor, again.
- **Manual-approval-required stages** — e.g. the final 100% cutover.
- **Cost-delta ceiling** — rejects a canary that would raise spend beyond a percentage.
- **Right-sizing auto-apply rules.**

Tested by `policies/tests/guardrails_test.rego` (`opa test policies/ -v`), plus adversarial tests that confirm a forged verdict, a freeze-window bypass and a micro-sample attack are all blocked.

**One deliberate asymmetry:** if OPA is unreachable, the system **fails open for rollback** and **fails closed for promotion**. An outage can never promote a bad build, but it can never trap a broken one either.

---

## 7. Data model — every table

14 tables. Every tenant-scoped one has RLS **enabled and forced** (applies even to the table owner).

| Table | RLS | Purpose |
|---|---|---|
| `tenants` | — | Root anchor. No RLS: it *is* the tenant list. |
| `users` | — | Login must find a user across tenants *before* tenant context exists. Stores only a bcrypt hash. |
| `auth_events` | — | Login/refresh/logout audit. A failed login with an unknown email has no tenant to scope by. |
| `projects` | ✅ | **Phase 8.** Repo/build/image metadata from the creation wizard. Owns a `pipelines` row. |
| `pipelines` | ✅ | The declarative policy YAML the worker executes. |
| `pipeline_executions` | ✅ | One row per run. Carries `project_id`, `trigger_type`, `commit_sha`, `commit_message`. |
| `execution_state` | ✅ | Durable live state for crash recovery — **the authoritative final status** (see the trap in §16). |
| `stage_logs` | ✅ | Durable per-stage output. **Table exists; nothing writes to it yet.** |
| `verification_records` | ✅ | Every verdict + evidence JSONB + HMAC + RCA summary. The source of truth for verdicts. |
| `audit_ledger` | ✅ | Every actuation, written **before** the action executes. |
| `policy_rules` | ✅ | Versioned Rego snapshots per pipeline. |
| `approvals` | ✅ | Manual gate sign-offs. |
| `cost_analysis` | ✅ | Per-run cost delta. Only written when a cost stage runs. |
| `alembic_version` | — | Migration bookkeeping. |

### The central relationship

```
tenants ─┬─ projects ──────┐
         ├─ pipelines ◄────┘  (a project OWNS a pipeline)
         └─ pipeline_executions ──┬─ verification_records
                                  ├─ audit_ledger
                                  ├─ execution_state
                                  ├─ stage_logs
                                  ├─ approvals
                                  └─ cost_analysis
```

**Why a project *wraps* a pipeline rather than replacing it:** every one of those child tables foreign-keys to `pipeline_executions`. If projects had their own separate runs table, a project's verdicts and audit rows would land somewhere nothing else joins to — the Verification Inspector and Audit Ledger would show nothing. Wrapping means **a project rollout is an ordinary pipeline execution**, so the entire existing chain works untouched.

Migrations live in `migrations/versions/` (`0001`–`0007`). The same DDL is mirrored in `services/api-gateway/src/db/schema.sql`, which initialises a fresh database.

---

## 8. Security model — four independent layers

Each layer holds even if the ones above it fail.

### Layer 1 — Authentication
- bcrypt password hashing.
- **HS256 JWTs whose signature is actually verified** (an earlier version carried the claim unverified — a forgeable-token hole, now closed).
- 15-minute access tokens; 7-day **opaque refresh tokens held in Redis as a revocable allow-list**, rotated on every use and revoked on logout.
- Login rate limiting: 5 failures → 429 lockout.
- Every auth event recorded in `auth_events`.

### Layer 2 — Authorization
`require_role()` is actually checked (not merely carried in the token) on: pause, resume, rollback, policy save, service onboarding, project create/delete.

### Layer 3 — Tenant isolation (Postgres RLS)
Enforced by the **database**, not by application `WHERE` clauses, so a forgotten filter cannot leak data. Verified live with two tenants: tenant B sees zero of tenant A's projects and gets `404` (not `403` — existence is not leaked) on direct IDs.

Two non-negotiables here:
- Services connect as **`app_user`** (`APP_POSTGRES_DSN`), never the `platform` superuser — a superuser bypasses RLS unconditionally.
- Route handlers depend on **`get_request_db`**, never `get_db` — with `NullPool` a fresh session is a different physical connection, so the middleware's tenant context would not apply.

### Layer 4 — Verdict integrity
Every verdict is HMAC-SHA256 signed by `verification-engine` and signature-verified (plus a freshness window) by `policy-controller` before OPA ever sees it. Combined with the structural separation (§4), forging a deployment decision requires compromising two services *and* the shared signing key.

---

## 9. The user interface

React 18 + TypeScript + Vite + Tailwind + Radix primitives + TanStack Query + Zustand + Recharts. Dark/light themed.

> **There is exactly one authenticated console.** The old `/app` pipeline console was retired; `/app/*` now redirects to `/projects`.

### Routes

| Route | Screen |
|---|---|
| `/` | Landing page |
| `/login` | Sign in |
| `/projects` | **Overview** — service card grid |
| `/projects/new` | **3-step creation wizard** |
| `/projects/:id/pipeline` | Pipeline View |
| `/projects/:id/verification` | Verification Inspector |
| `/projects/:id/policy` | Policy & Gates |
| `/projects/:id/audit` | Audit Ledger |

### Overview (`/projects`)
A card per service: name, repo + branch, live status badge (`HEALTHY` / `CANARY RUNNING · 25%` pulsing / `FAILED`), 7-day success rate, mean time to verify, last deploy with commit SHA, and a `Trigger Rollout` action.

Every number is computed from real data. A project with no finished runs shows `—`, never a filler value. **There is no cost-delta stat** because nothing computes one for these runs — a placeholder would be a fabricated metric.

### Creation wizard (`/projects/new`)
1. **Choose Repository** — `Git Provider` tab (Connect GitHub via OAuth, then pick from your real repos, private ones flagged with a lock) or `Public Git Repository` tab (paste a clone URL).
2. **Configure Build & Test** — branch (live from the GitHub API), root directory, Dockerfile path, test command, registry image, baseline/canary tags, plus port / health-check path / traffic path prefix.
3. **Progressive Policy & Deploy** — canary preset 10→25→50→100, golden-signal assertions, and the guardrail sliders (confidence floor, min sample size, max cost delta).

Creating a project generates its pipeline YAML, registers the pipeline, and (best-effort) applies the real Kubernetes objects. If the cluster is unreachable the project is still created and the response says so — failing the whole creation would make the feature unusable without a cluster.

### Project workspace (`/projects/:id`)
A sub-header with the project identity and `Trigger New Rollout` / `Pause` / `Emergency Rollback`, a Build → Test → Progressive Canary stepper, a run selector, and four tabs:

- **Pipeline View** — stage DAG (click a stage to filter the log), traffic gauge, weight-over-time chart, and the live streaming execution log.
- **Verification Inspector** — confidence gauge, composite score, verdict badge, per-metric evidence (SPRT boundary chart, Mann-Whitney comparison, generic cards for other categories), and the AI root-cause analysis.
- **Policy & Gates** — CodeMirror YAML editor with live OPA-backed validation, plus read-only derived guardrail values.
- **Audit Ledger** — summary cards and an expandable table of this project's signed actuations. Scoped to the project; the SOC 2 export is tenant-wide and its button says so.

**How the four tabs are shared:** the workspace supplies `{pipelineId, pipelineRunId, tenantId, projectId, hideRunControls}` as router outlet context. The screens are ordinary components that read it via `useAppContext()` — they need no project-specific variants. The contract lives in `frontend/src/types/app-context.ts`.

---

## 10. GitHub integration

**Connection is a real OAuth 2.0 Authorization Code flow**, not a pasted token.

```
user clicks "Connect GitHub"
   → GET /authorize-url   (authenticated; mints a single-use `state` in Redis bound to this user)
   → browser redirects to github.com, user authorizes
   → GET /callback?code&state   (UNAUTHENTICATED by design — a top-level browser
                                 navigation carries no Authorization header; the
                                 `state` re-establishes identity AND is the CSRF defence)
   → server-side code→token exchange (client secret never reaches the browser)
   → token stored in Redis keyed by user id, 30-day TTL
   → browser bounced back to /projects/new?github=connected
```

**Token storage: Redis, never Postgres.** This service has no `cryptography` dependency, so a DB column would mean a plaintext OAuth token at rest in the tenant database. Redis matches how refresh tokens are already handled; a restart just means reconnecting. The token is never logged and never sent to the browser.

Resolution order for GitHub API calls: `X-GitHub-Token` header → the user's stored OAuth token → server-wide `GITHUB_TOKEN`.

### Setup (required before the button works)
1. GitHub → Settings → Developer settings → **OAuth Apps** → New OAuth App
2. Homepage `http://localhost:3000`; **Authorization callback URL** `http://localhost:8000/api/v1/integrations/github/callback` — must match `GITHUB_OAUTH_REDIRECT_URI` exactly or GitHub returns `redirect_uri_mismatch`
3. Set `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` in `.env`, then `docker compose up -d api-gateway`

Until configured, the wizard shows those instructions on screen and the Public Git Repository tab still works.

### Known limitation — private repository builds
OAuth solves repository **discovery and selection**. The **build clone** of a private repo still uses the server-side `GITHUB_TOKEN` on `pipeline-worker`, because the user's OAuth token is deliberately not written into pipeline YAML (that would put a live secret in the database). Public repos work fully via OAuth today.

The fix, in ascending order of correctness: (1) a short-lived per-run credential brokered to the worker; (2) clone in api-gateway and hand the worker an artifact; (3) **a GitHub App with installation tokens** — 1-hour expiry, per-repo scope, revocable by uninstall, and the only option that also enables `GIT_PUSH_WEBHOOK` triggers. (3) is the destination; it needs `cryptography` added for RS256 JWT signing.

---

## 11. Complete API reference

All endpoints require `Authorization: Bearer <token>` except where noted.

### Auth
| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/login` | Unauthenticated. Returns access + refresh tokens. |
| POST | `/api/v1/auth/refresh` | Unauthenticated. Rotates both tokens. |
| POST | `/api/v1/auth/logout` | Revokes the refresh token. |

### Projects (Phase 8)
| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/api/v1/projects` | any | Cards payload: latest run, verdict, 7-day stats |
| POST | `/api/v1/projects` | lead-sre | Creates project + pipeline, best-effort cluster provisioning |
| GET | `/api/v1/projects/{id}` | any | Detail + latest run |
| DELETE | `/api/v1/projects/{id}` | lead-sre | Cascades runs |
| POST | `/api/v1/projects/{id}/rollout` | any | Starts a run |
| GET | `/api/v1/projects/{id}/runs` | any | History, with verdict joined |
| GET | `/api/v1/projects/{id}/runs/{run}/stages` | any | Build/Test/Canary stepper state |
| GET | `/api/v1/projects/{id}/runs/{run}/logs/stream` | any | SSE; `?stage=` filter |
| POST | `/api/v1/projects/{id}/runs/{run}/rollback` | lead-sre | Goes through HMAC + OPA, same as autonomous |
| GET | `/api/v1/projects/{id}/audit` | any | Project-scoped ledger |

### GitHub
| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/integrations/github/status` | Connected? As whom? Never returns the token |
| GET | `/api/v1/integrations/github/authorize-url` | Mints the `state` |
| GET | `/api/v1/integrations/github/callback` | **Unauthenticated** — allowlisted in middleware |
| POST | `/api/v1/integrations/github/disconnect` | Forgets token + profile |
| GET | `/api/v1/integrations/github/repos` | `?search=` |
| GET | `/api/v1/integrations/github/repos/{owner}/{repo}/branches` | |
| GET | `/api/v1/integrations/github/repos/{owner}/{repo}/commits/{ref}` | HEAD commit for provenance |

### Pipelines (lower-level; still supported)
`GET|POST /api/v1/pipelines` · `POST|GET /api/v1/pipelines/{id}/runs` · `GET /api/v1/pipelines/runs/{run}` · `GET /api/v1/pipelines/{run}/logs/stream` · `POST /api/v1/pipelines/{run}/pause|resume|rollback` (lead-sre)

### Verification, policy, audit, reports, services
`GET /api/v1/verification/{run}` · `GET /api/v1/verification/{run}/history` · `POST /api/v1/policies` (lead-sre) · `POST /api/v1/policies/validate` · `GET /api/v1/policies/{pipeline_id}` · `GET /api/v1/audit` · `GET /api/v1/audit/export/soc2` · `GET /api/v1/reports/deployment/{run}` · `GET /api/v1/reports/digest/{tenant}` · `POST /api/v1/services` (lead-sre)

### Non-REST
`WS /ws/pipelines/{run}` — live state · `GET /healthz`, `/readyz`, `/metrics` — unauthenticated

---

## 12. Configuration — every environment variable

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_DSN` | `platform:platform@postgres` | **Superuser — migrations only.** Bypasses RLS. |
| `APP_POSTGRES_DSN` | `app_user:app_password@postgres` | **What services must use.** Non-superuser, so RLS applies. |
| `REDIS_URL` | `redis://redis:6379/0` | State, streams, tokens, locks |
| `OPA_URL` | `http://opa:8181` | Policy decisions |
| `JWT_SECRET_KEY` | `change-me-in-prod` | Signs/verifies access tokens |
| `VERDICT_SIGNING_KEY` | `dev-signing-key...` | **Must match across verification-engine and policy-controller** (see §16) |
| `PROMETHEUS_URL` | `http://prometheus:9090` | Real telemetry source |
| `GROQ_API_KEY` | *(empty)* | RCA text; falls back to deterministic evidence summary |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | |
| `SLACK_WEBHOOK_URL` | *(empty)* | Alerts; logged instead when blank |
| `GITHUB_CLIENT_ID` / `_SECRET` | *(empty)* | OAuth App; wizard degrades gracefully when unset |
| `GITHUB_OAUTH_REDIRECT_URI` | `http://localhost:8000/api/v1/integrations/github/callback` | Must match GitHub exactly |
| `FRONTEND_BASE_URL` | `http://localhost:3000` | Where the OAuth callback returns the browser |
| `GITHUB_TOKEN` | *(empty)* | Fallback token; also what `pipeline-worker` uses to clone private repos |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | |
| `KUBECONFIG` | per-service | Mounted only into worker + controller — **never** verification-engine |

---

## 13. Testing

```bash
make test-all          # stats + engine + guardrails + OPA
make test-engine       # cd services/verification-engine && pytest tests/ -v
make test-stats        # tests/adversarial/test_statistical_robustness.py
make test-guardrails   # tests/adversarial/test_guardrail_bypass.py
make test-opa          # opa test policies/ -v
```

Frontend (from `frontend/`):
```bash
npm run test           # Vitest — 12 unit tests
npm run test:e2e       # Playwright golden path + onboarding (needs the stack up)
```

**Adversarial tests are the interesting ones** — they assert that a forged verdict, a freeze-window bypass and a micro-sample attack are all *blocked*, rather than that the happy path works.

> On Windows, the repo path contains an `&`, which breaks every `node_modules/.bin/*.cmd` shim (cmd.exe treats `&` as a command separator). Work around it by invoking node directly: `node node_modules/vite/bin/vite.js build`, `node node_modules/typescript/bin/tsc -b`.

---

## 14. What is real, and what is not

This section exists so nobody is misled by a demo.

### Genuinely real, verified live
- Statistical tests on real Prometheus data, correctly routed by category.
- HMAC-signed verdicts, signature actually verified before OPA.
- OPA guardrails, adversarially tested.
- Real Kubernetes: Kind + Envoy Gateway, real `HTTPRoute` weight shifts, real rollback (canary scaled to 0, **zero baseline pod restarts**), confirmed by measuring the actual proportion of canary responses.
- Postgres RLS isolating two tenants over real HTTP.
- Real auth: bcrypt, verified JWTs, revocable refresh tokens, rate limiting, audit trail.
- Redis Streams consumer groups: 3 replicas × 10 concurrent rollouts, zero duplicates, zero drops.
- Crash recovery: a killed-mid-rollout pipeline genuinely resumes.
- Real `git clone` → real `docker build` → canary, driven from the project wizard.
- A project rollout producing a real `HEALTHY` verdict (confidence 0.857, SPRT LLR −3.04) → OPA `PROMOTE_STEP` → real traffic shift to 25%, recorded in the audit ledger.

### Real but partial
- **Telemetry** — real for pipelines that declare a `prometheus` query block. Others fall back to a synthetic generator, which is explicit and logged (`verification_using_synthetic_fallback_telemetry`), not silent.
- **RCA** — real Groq calls only when `GROQ_API_KEY` is set; otherwise a deterministic evidence summary.
- **Charts** — built from the summary statistics the verdict actually carries. A true empirical CDF or full LLR trajectory would need the backend to return raw per-request samples.

### Not built
- `stage_logs` table exists and is indexed, but **nothing writes to it** — Redis remains the log path (24h TTL).
- **MinIO** is running but unused.
- **`GIT_PUSH_WEBHOOK`** is a valid `trigger_type` value, but nothing produces it — that needs a GitHub App.
- **Celery** — the spec names it for `pipeline-worker`; the implementation runs stages as synchronous calls behind a Redis Streams consumer group.
- **Private-repo OAuth cloning** — see §10.
- **TLS and a real secrets store** — deferred to Phase 7 (deployment-dependent).
- **Cost analysis** — the table and OPA rule exist; no stage populates it for these pipelines.

---

## 15. Repository map

```
├── README.md                  Quick start
├── ARCHITECTURE.md            Short design-decision write-up
├── SYSTEM_GUIDE.md            ← this file
├── MASTER_BUILD_SPEC.md       Original design spec
├── CLAUDE.md                  Contributor notes + invariants
├── docker-compose.yml         16 services
├── Makefile                   make help
├── docs/roadmap/              Phase 1–8 trackers (00-ROADMAP.md is the index)
├── migrations/versions/       Alembic 0001–0007
├── policies/                  delivery_guardrails.rego + tests
├── pipelines/                 Declarative pipeline YAML
├── k8s/                       Kind config, Envoy Gateway, canary manifests
├── monitoring/                Prometheus, Loki, Promtail, Grafana provisioning
├── shared/                    logging_config.py, redis_streams.py (mounted into every service)
├── services/
│   ├── api-gateway/           FastAPI: auth, routers, db, websocket
│   ├── pipeline-worker/       DAG execution, tasks, k8s onboarding, reconciler
│   ├── verification-engine/   Statistics, scoring, signing (no k8s)
│   ├── policy-controller/     Verify → OPA → actuate → audit
│   ├── explainability-service/ Grounded RCA
│   └── sample-app/            v1.0.0 baseline / v1.1.0 canary
├── frontend/
│   ├── src/api/               Typed clients (client, projects, github, auth, …)
│   ├── src/pages/             Landing, Login, ProjectsOverview, NewProject,
│   │                          ProjectWorkspace + the 4 tab screens
│   ├── src/layouts/           ProjectsLayout
│   ├── src/components/        ui/ (Radix), charts/, pipeline/, verification/
│   ├── src/hooks/             usePipelineEvents (WS), useLiveLogs (SSE),
│   │                          useVerificationResult, useAuditLog, useAppContext
│   ├── src/types/             app-context.ts (the outlet-context contract)
│   └── e2e/                   Playwright
└── tests/adversarial/         Forged verdicts, guardrail bypass, micro-samples
```

---

## 16. Troubleshooting — traps that have bitten before

Each of these cost real debugging time. They are documented so they cost it only once.

**Verdicts silently rejected as forged.** `VERDICT_SIGNING_KEY` drifted between containers — docker-compose only re-reads `.env` at container *creation*, so restarting one service after an `.env` edit is not enough. Check first:
```bash
docker compose exec verification-engine env | grep VERDICT_SIGNING_KEY
docker compose exec policy-controller  env | grep VERDICT_SIGNING_KEY
```

**Every RLS query returns zero rows.** Either the policy was declared `AS RESTRICTIVE` (a RESTRICTIVE policy with no companion PERMISSIVE one denies everything to everyone — use plain `FOR ALL USING (...)`), or the service connected via the `platform` superuser DSN instead of `APP_POSTGRES_DSN`.

**RLS silently never applies.** A route handler used `Depends(get_db)` instead of `get_request_db`. With `NullPool` that is a different physical connection from the one the middleware scoped.

**`SET LOCAL x = :val` is a syntax error.** Postgres will not accept a bind parameter there. Use `SELECT set_config('x', :val, true)`.

**Every finished run shows `PENDING`, every success rate 0%.** `pipeline_executions.status` is written once at trigger time and never updated; the durable final status lives in `execution_state`. Read `COALESCE(execution_state.status, pipeline_executions.status)`.

**The live log panel shows "Waiting for log output…" forever.** `EventSource` cannot send an `Authorization` header, so the SSE stream was rejected 401. Stream over `fetch` instead. Also: `sse_starlette` emits CRLF, so frames are separated by `\r\n\r\n` — a parser splitting on `\n\n` matches nothing and buffers forever.

**A public repo fails to clone.** The generated build stage named `repoCredentialsEnvVar` unconditionally, and `git_clone.py` correctly refuses when that variable is unset rather than silently cloning anonymously. Only emit it for private repos.

**`patch_namespaced_custom_object(..., _content_type=...)` does nothing** in `kubernetes==29.0.0` — that kwarg is not accepted by this client version's generated binding, which hardcodes merge-patch. Call `ApiClient.call_api(...)` directly to preserve true RFC 6902 JSON Patch semantics.

**GitHub returns `redirect_uri_mismatch`.** The callback URL registered on the OAuth App must match `GITHUB_OAUTH_REDIRECT_URI` character for character.

---

## Design invariants — do not violate these

1. **No static thresholds.** Every decision comes from a statistical test or a composite derived from them.
2. **Metric routing is by declared `category`** — never a hardcoded per-metric branch.
3. **`verification-engine` must never gain Kubernetes access.** Its sandboxing is enforced by the absence of the dependency.
4. **Verdicts are signed, and the signature is verified** before OPA is consulted.
5. **Traffic shifts go through the `HTTPRoute`**, never by changing replica counts — that is what avoids pod restarts.
6. **Multi-tenancy is enforced by Postgres RLS**, never by an application-layer convention.
7. **The sample floor N ≥ 100 is enforced in two places** — `confidence.py` and the OPA policy. Never lower one alone.
