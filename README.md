# Smart AI DevOps & Continuous Delivery Platform

CI/CD that verifies its own deployments — the platform statistically compares a canary's live behavior against a baseline and autonomously promotes or rolls back, inside guardrails it cannot cross by construction.

Built for Assignment 05 (AI-Native Build Track). See `MASTER_BUILD_SPEC.md` for the full design spec and `CLAUDE.md` for contributor-facing architecture notes.

## What this is

Most CI/CD tooling stops at "run the pipeline" — a human has to watch a dashboard after every deploy. This platform replaces that human judgment call with:

1. A **verification engine** that runs genuine statistical tests (SPRT, Mann-Whitney U, Kolmogorov-Smirnov, CUSUM, BOCPD, Fisher's exact/χ², Isolation Forest) comparing canary telemetry against a baseline cohort — never a static threshold check.
2. A **policy controller** that only ever acts on a verdict it can cryptographically prove came from the verification engine, gated by an explicit OPA policy (freeze windows, minimum confidence, minimum sample size, required approvals).
3. An **explainability service** that turns every decision into a plain-language explanation grounded in the exact numbers that drove it.
4. A **delivery-ops UI** with four screens: live pipeline view, verification detail, policy/gates config, and audit ledger.

## Quick start

Requires Docker Desktop.

```bash
git clone <this-repo>
cd "Smart AI DevOps & Continuous Delivery Platform"
cp .env.example .env        # fill in GROQ_API_KEY if you want live RCA text (optional — has a deterministic fallback)
docker compose up -d --build
```

This brings up the full stack: Postgres, Redis, OPA, MinIO, Prometheus, Loki+Promtail+Grafana, the 5 backend services (`api-gateway:8000`, `pipeline-worker:8001`, `verification-engine:8002`, `policy-controller:8003`, `explainability-service:8004`), the frontend at `localhost:3000`, and the sample-app baseline/canary pair + load generator.

**Platform observability** (Phase 6): Grafana at `localhost:3001` (`admin`/`admin`) comes with Prometheus and Loki already wired as datasources and a "Platform Health" dashboard auto-loaded — queue depth, request latency percentiles, verdict outcome distribution, service readiness. Every backend service's structured JSON logs are shipped to Loki automatically; a single `pipeline_run_id`'s logs across all 4 services it touches are one LogQL query away via the `trace_id` field every log line carries (`{service=~".+"} | json | trace_id="<id>"`).

Check everything is healthy:

```bash
docker compose ps
curl localhost:8000/healthz && curl localhost:8000/readyz
```

Full interactive API reference (OpenAPI/Swagger) for the gateway is live at **`localhost:8000/docs`** — every other service also exposes its own `/docs`.

### Standing up the real Kubernetes target (optional but recommended)

By default, `pipeline-worker`'s demo pipeline only exercises the statistics/policy core (§"Known limitations" below). To see the platform actually drive a real progressive-delivery rollout — genuine traffic-weight shifting on a real `HTTPRoute`, genuine rollback via a real `Deployment` scale-down, zero pod restarts — do this once:

```bash
make kind-up           # creates a 3-node Kind cluster
make install-envoy     # installs Envoy Gateway + Gateway API CRDs
make deploy-sample-app # builds/loads sample-app images, applies gateway + canary manifests
make connect-kind-network   # wires pipeline-worker/policy-controller to the live cluster
docker compose restart pipeline-worker policy-controller
```

Verify: `curl localhost:8001/readyz` should report `"kubernetes": "ok"`. From here, triggering a rollout that produces a `HEALTHY` verdict will genuinely shift `HTTPRoute` traffic (`kubectl get httproute payment-service-route -n production -w`), and a `FAILED` verdict will genuinely scale the canary `Deployment` to 0.

### Using the UI

Open `localhost:3000` and log in with the seeded demo account: `demo@acme-corp.test` / `acme-demo-2026` (a second tenant is seeded too: `demo@other-corp.test` / `other-demo-2026`, to see tenant isolation for yourself — it has no pipelines, so it should show an empty state, never the first tenant's data). Pick a pipeline and run from the dropdowns in the header (no more pasting IDs), click **Trigger New Rollout**, then check the **Verification Inspector** tab for the resulting verdict and evidence.

### Running the test suites

```bash
# Per-service unit tests
cd services/verification-engine && python -m pytest tests/ -v
cd services/policy-controller && python -m pytest tests/ -v
cd services/pipeline-worker && python -m pytest tests/ -v
cd services/explainability-service && python -m pytest tests/ -v

# OPA policy tests
opa test policies/ -v          # bin/opa.exe on Windows

# Cross-service adversarial tests (forged verdicts, freeze-window bypass, etc.)
python -m pytest tests/adversarial/ -v
```

All of the above are green as of this build (89+ unit/integration tests — including OPA-unreachable-failsafe and signing-key-rotation coverage — 4/4 OPA tests, 24/24 adversarial tests — including live RBAC/rate-limiting/refresh-token attacks against a running api-gateway, and a static + live check that verification-engine still has no path to Kubernetes — plus a real crash-recovery test against real Redis+Postgres).

## Architecture

See `ARCHITECTURE.md` for the one-page design-decisions summary, and `CLAUDE.md` for the full contributor-facing map (invariants, gotchas already hit and fixed, per-service responsibilities).

```
Trigger →  api-gateway  → (Redis) → pipeline-worker
                                          ↓ (HTTP)
                                 verification-engine  (runs the statistics, signs the verdict)
                                          ↓ (Redis pub/sub, HMAC-signed)
                                 policy-controller     (verifies signature → asks OPA → actuates or blocks)
                                          ↓
                                 explainability-service (grounded RCA via Groq, with fallback)
```

Every one of the 5 services is independently containerized (`services/*/Dockerfile`), health-checked (`/healthz` + `/readyz`), and structured-JSON-logs via a shared logging module (`shared/logging_config.py`).

## Known limitations

Being direct about where this currently falls short of "production," in priority order:

1. ~~Telemetry is synthesized~~ — **fixed.** A real Prometheus instance scrapes real request/latency metrics from two running `sample-app` containers (`sample-app-baseline`/`sample-app-canary`, plus a `load-generator` producing continuous real traffic), and `verification-engine`'s `/verify` pulls genuine PromQL data (`services/verification-engine/src/telemetry/prometheus_client.py`) for any pipeline whose metrics declare a `prometheus` query block — verified live: real error/latency injection into the canary produced a real `FAILED` verdict and a real autonomous rollback. The old synthetic generator is now an explicit, logged fallback for pipelines that haven't been wired to a metrics source, not the default path.
2. ~~No real Kubernetes cluster has canary traffic actually shifted on it~~ — **fixed and verified.** A real Kind cluster (1 control-plane + 2 workers) runs Envoy Gateway, and `payment-service-baseline`/`payment-service-canary` are real Deployments behind a real `HTTPRoute`. `pipeline-worker` and `policy-controller` are connected to the cluster's Docker network with a working in-cluster kubeconfig (`k8s/generated/incluster-kubeconfig.yaml`), and `actuation_executor.py`'s weight-patching was verified against the live API: an autonomous `HEALTHY` verdict shifted real traffic (confirmed 100/0 → 75/25 → measured ~25% of real HTTP requests actually landing on the canary pod), and an autonomous `FAILED` verdict (SPRT rejecting a genuine injected error rate) triggered a real rollback — `HTTPRoute` weight back to 0%, canary `Deployment` scaled to 0 replicas, canary pods actually terminated — with zero manual intervention and zero baseline pod restarts. This also caught and fixed two real bugs the demo path never exercised: `patch_namespaced_custom_object`'s JSON-Patch content-type kwarg doesn't exist in `kubernetes==29.0.0` (was silently never callable), and a stale `VERDICT_SIGNING_KEY` on one container caused every verdict to be rejected as forged.
3. ~~Auth is not production-grade~~ — **fixed and hardened.** Login (`POST /api/v1/auth/login`) checks a bcrypt-hashed password and issues a real HS256-signed 15-minute access token plus a revocable 7-day refresh token; `auth/middleware.py` verifies the access token's signature and now also checks its `role` claim (`src/auth/rbac.py`) before letting a `developer` pause/rollback a pipeline or onboard a service — those require `lead-sre`+. Five failed logins in a row locks the account out for 5 minutes (Redis-backed), and every login/refresh/logout is recorded in a queryable `auth_events` table. Still not "enterprise-grade": no password-reset flow and no SSO (explicitly out of scope for this assignment); TLS termination and moving off a plaintext `.env` are deferred to Phase 7 (deployment-target-dependent — see `docs/roadmap/04-security-hardening.md`).
4. ~~`pipeline-worker` doesn't use Celery... won't scale to concurrent pipeline execution across multiple workers~~ — **the scaling gap is fixed.** It still doesn't use Celery (stages still run as plain synchronous function calls), but triggering now goes through a Redis Streams consumer group instead of bare pub/sub — verified live running 3 replicas each of `pipeline-worker`/`policy-controller` and firing 10 concurrent rollouts across 2 services with zero duplicate processing and zero dropped triggers (see `docs/roadmap/05-reliability-scale.md`). Crash recovery is also now real: a pipeline-worker killed mid-rollout has its interrupted run resumed by another replica, verified by a real test, not just by reading `reconciler.py`.
5. **MinIO is running but unused** — no service currently writes artifacts to it.
6. **No `tests/e2e/` suite or `scripts/demo/*.sh`** — the phased build plan in `MASTER_BUILD_SPEC.md` §15 calls for these; adversarial and per-service tests exist and pass, but there's no scripted "trigger a healthy rollout end-to-end" / "trigger a failing one and watch it roll back" demo script yet.
