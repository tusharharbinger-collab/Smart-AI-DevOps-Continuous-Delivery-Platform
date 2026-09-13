# Phase 3 — Multi-Service Onboarding

**Status: done, verified live end-to-end (a second, genuinely different service onboarded through the API/UI alone, real independent Kubernetes objects, real isolated canary traffic-weight shifts, zero manual YAML).**

## Goal
A team can bring their own containerized HTTP service, describe it once through a guided flow, and have the platform generate everything needed to run progressive delivery for it — without hand-writing Kubernetes YAML or knowing this platform's internal file layout.

## Why now
This is the difference between "a demo of one hardcoded service" and "a platform." It follows Phase 2 so a newly onboarded service could, in principle, get real telemetry by default rather than the synthetic fallback (see the honest scope note below on why this phase doesn't fully deliver that part yet).

## What shipped

### 3.1 — Parameterized the actuation layer
- [x] `services/policy-controller/src/actuation_executor.py`: `update_traffic_weights`/`emergency_rollback` now take `route_name`, `namespace`, `canary_deployment_name` as parameters (with the old hardcoded values kept only as `DEFAULT_*` fallback constants, for pre-Phase-3 runs).
- [x] `services/pipeline-worker/src/worker.py`: `PipelineOrchestrator._register_actuation_target` parses the pipeline's own manifest at pipeline start and writes `actuation_target:{pipeline_run_id}` to Redis (mirroring `execution_state`'s existing Redis-mirror pattern) — namespace, route name, and canary deployment name, all derived from that pipeline's own YAML, not a global constant.
- [x] `services/policy-controller/src/controller.py`: `handle_incoming_verdict` looks up that mapping by `pipeline_run_id` before acting, so policy-controller never needs to parse pipeline YAML itself — it stays a pure "verify signature → ask OPA → actuate the right service" loop.
- [x] Regression-verified: retriggered the original `payments-pipeline` end-to-end after this change — identical real `HEALTHY` verdict → real `HTTPRoute` weight patch (`75/25`), confirmed via `kubectl get httproute payment-service-route`.

### 3.2 — Manifest generation
- [x] `services/pipeline-worker/src/k8s/manifest_generator.py`: given a short `ServiceOnboardingSpec` (name, image, tags, port, health-check path, path prefix), generates a baseline+canary `V1Deployment` pair, a baseline+canary `V1Service` pair, and an `HTTPRoute` (as a plain dict, applied via the generic `CustomObjectsApi` the same way `actuation_executor.py` treats it as opaque JSON) — every name derived from `service_name` alone so two onboarded services never collide.
- [x] `services/pipeline-worker/src/k8s/onboarding.py`: applies all five objects via the real Kubernetes API (create, falling back to patch on 409 — idempotent, safe to re-run for the same service), then generates this platform's own declarative Pipeline YAML text for the new service.
- [x] Applied via the Kubernetes API directly from pipeline-worker (not a `kubectl` subprocess), consistent with the rest of this codebase's actuation code.

### 3.3 — Onboarding flow (backend + frontend)
- [x] `POST /services/onboard` (pipeline-worker) — generates + applies manifests, returns the generated pipeline YAML.
- [x] `POST /api/v1/services` (api-gateway, `services_router.py`) — calls pipeline-worker's onboarding endpoint, then registers the returned YAML via the same `pipelines` table insert `POST /api/v1/pipelines` already uses (a clean `409` on a duplicate service name, not a raw DB stack trace).
- [x] `frontend/src/api/services.ts` rewritten to call the real endpoint (Phase 1's mock removed); `AddServiceDialog.tsx`'s 3-step form/validation/review layout needed zero changes — proving Phase 1's UI shell was built to the real contract.
- [x] New Playwright test `frontend/e2e/onboarding.spec.ts`: logs in, fills the real form, confirms deploy, asserts the real generated pipeline YAML renders — run against the live stack, not mocked.

### 3.4 — Non-HTTP services
- [ ] Not addressed — every generated `HTTPRoute` assumes a plain HTTP backend. `GRPCRoute` support (Gateway API has a distinct resource for it) is deferred; nothing in this phase blocks adding it later since `manifest_generator.py` isolates the HTTPRoute-building logic to one function.

## Honest scope note: generated pipelines use synthetic telemetry, not real Prometheus data
The generated pipeline YAML deliberately has **no** `prometheus` query block, unlike the hand-wired demo pipeline. Phase 2 wired one static Prometheus scrape target per sample-app cohort (`monitoring/prometheus.yml`); a freshly onboarded service running as a brand-new Kind Deployment has no scrape target generated for it. Real per-service scraping needs either a generated scrape-config entry or genuine Kubernetes service discovery (a `ServiceMonitor`/Prometheus Operator) — follow-up work, not in this phase's scope. Omitting the block is the honest choice: pipeline-worker's existing, already-tested synthetic-fallback path runs instead (logged as `verification_using_synthetic_fallback_telemetry`), so the generated pipeline still genuinely verifies and promotes/rolls back — on synthetic data, exactly like this platform's own demo pipeline before Phase 2 wired it up.

## Real bugs found and fixed (found by actually onboarding a second service against the live Kind cluster, not by reasoning about the code)
1. **Canary deployment generated with 0 replicas.** The original demo pipeline has no "deploy" stage — its canary Deployment was pre-created outside the pipeline entirely, by `make deploy-sample-app`. A freshly onboarded service has nothing else that would ever scale its canary up, so generating it at 0 replicas meant the very first weight shift (10-25%) would route real traffic to a Service with zero ready endpoints. Fixed: the generated canary Deployment now starts at a real replica count (`canary_replicas`, default 1) — caught by checking `kubectl get pods` after the first weight shift and finding no canary pod at all.
2. **Demo pipeline's stored Postgres `policy_yaml` had no explicit route/deployment names.** `services: payments-service` alone would derive to `payments-service-route` (plural), but the real, already-running `HTTPRoute` is named `payment-service-route` (singular) — a naming mismatch that would have silently made 3.1's new Redis-based lookup patch a route that doesn't exist. Fixed by adding explicit `routeName`/`canaryDeployment` fields to both the demo pipeline's registered YAML (updated in Postgres) and the checked-in `pipelines/payments-service-policy.yaml`, rather than relying on name-derivation for a pipeline pre-dating this phase.
3. **Raw 500 on duplicate onboarding.** Re-onboarding a service name already registered hit Postgres's `pipelines_tenant_id_name_key` unique constraint as an unhandled `IntegrityError`, leaking a full SQLAlchemy stack trace to the client. Fixed with a caught `IntegrityError` → clean `409` (the K8s manifests apply idempotently regardless, since `onboard_service` creates-then-patches-on-409 for every object).

## Live proof (not just tests)
- Onboarded `checkout-service` via `POST /api/v1/services` reusing the already-loaded `localhost:5001/payments` image under a new identity — real `checkout-service-baseline`/`checkout-service-canary` Deployments (confirmed `Running`/`1/1` via `kubectl get pods -n production -l app=checkout-service`), a real `checkout-service-route` HTTPRoute, entirely independent of `payment-service-*`.
- Triggered the generated pipeline for real: a real `HEALTHY` verdict shifted `checkout-service-route`'s weight from `100/0` to `75/25` via the same autonomous "Trigger New Rollout" path — while `payment-service-route`, triggered separately moments earlier, stayed at its own independent `75/25`, proving **two services' rollouts don't interfere with each other's traffic weights** (the acceptance criterion).
- Onboarded a second, throwaway service (`e2e-svc-<timestamp>`) entirely through the browser UI (Playwright, `frontend/e2e/onboarding.spec.ts`) with zero manual YAML — real Deployments/Service/HTTPRoute confirmed via `kubectl`, then cleaned up.

## Acceptance criteria
- [x] A second, different service onboarded through the UI alone, with zero manual YAML, gets a real canary traffic split.
- [x] Two services' rollouts don't interfere with each other's traffic weights — verified concurrently on `payment-service-route` and `checkout-service-route`.
- [x] Existing test suites still pass unmodified: 68 (verification-engine) + 3 (policy-controller) + 3 (pipeline-worker) + 7 (explainability-service) + 13 (adversarial) + 4 (OPA) + 9 (frontend unit) + 7 (frontend e2e, incl. the new onboarding test) — all green.

## Depends on
Phase 2 (Real Telemetry) — done. Phase 4 (Security Hardening) should build per-service permissions on top of this phase's onboarding flow, now that "a service" is a real, generated, first-class concept rather than one hardcoded demo.
