# Phase 2 — Real Telemetry

**Status: done, verified live end-to-end (real error injection → real Prometheus scrape → real statistical FAILED verdict → real autonomous Kubernetes rollback).**

## Goal
The verification engine's statistical verdicts are computed from genuine, live-scraped metrics — not synthesized numbers standing in for them.

## Why now
Every other phase's value is capped by this one. A beautifully generalized onboarding flow (Phase 3) that still feeds the statistics fabricated data is a platform for demonstrating fake results faster.

## What shipped

### 2.1 — Real metrics source
- [x] **Prometheus** (`prom/prometheus:v2.55.1`) added to `docker-compose.yml`, config at `monitoring/prometheus.yml`.
- [x] `sample-app` v1.0.0 and v1.1.0 (both the root `sample-app/` and `services/sample-app/` copies — see note below) instrumented with `prometheus_client`: `payments_requests_total{outcome}` counter and `payments_request_duration_seconds` histogram, exposed at `/metrics`.
- [x] `sample-app-baseline` / `sample-app-canary` added to `docker-compose.yml` as real, independent containers (distinct from the Kind-cluster `payment-service-baseline`/`canary` Deployments used for the real-K8s traffic-split demo), plus a `load-generator` container so there's continuous real traffic to scrape. Prometheus scrapes both via static config, tagging each with an external `cohort` label.

### 2.2 — Telemetry client in verification-engine
- [x] `services/verification-engine/src/telemetry/prometheus_client.py` — `instant_query`/`range_query` primitives, plus category-specific fetchers: `fetch_counter_totals` (error_rate/business_metric numerator/denominator via `increase()`), `fetch_histogram_samples` (latency — see the approximation note below), `fetch_gauge_samples` (saturation). 7 unit tests, all passing.
- [x] `/verify` (`main.py`) now accepts `use_prometheus: bool` — when true, each metric's `prometheus` query block (`{cohort}`-templated) is resolved for both cohorts and used to populate the exact same `baseline_telemetry`/`canary_telemetry` shapes the dispatcher already expected. **No changes to any statistical test code** — SPRT/Mann-Whitney/KS/Fisher's-exact are unmodified; only the data feeding them changed.

### 2.3 — Retired the synthetic generator as the default
- [x] `pipeline-worker`'s `_synthesize_telemetry` is now an explicit, logged fallback (`verification_using_synthetic_fallback_telemetry`) used only when a pipeline's metrics carry no `prometheus` block; a wired pipeline logs `verification_using_real_prometheus_telemetry` instead and sends no synthesized samples at all.
- [x] The demo `payments-pipeline`'s registered YAML now has `prometheus` blocks for all 3 metrics (`http_error_rate`, `p95_latency_seconds`, `checkout_success_rate`), proving the whole loop end-to-end on genuine data via the same "Trigger New Rollout" button as before — nothing about the user-facing flow changed.

### 2.4 — Real-world telemetry messiness
- [x] `iqr_filter.py` unmodified and still applied to real reconstructed latency samples (verified: real KS/Mann-Whitney results above used it transparently).
- [x] Missing-Prometheus-data path verified: `_fetch_metric_from_prometheus` catches `PrometheusQueryError` and leaves that metric's telemetry keys unset rather than raising — `engine.py`'s existing "insufficient samples → score 100, not held against the canary" handling took over unchanged (hit for real during debugging, see bugs below — confirmed graceful, not a crash).

## Honest scope note: latency "samples" are a documented approximation
Prometheus histograms only expose cumulative bucket *counts*, never literal per-request values — Mann-Whitney/KS need raw values. `fetch_histogram_samples` reconstructs an approximate sample array by attributing each observation in bucket `(prev_le, le]` to that bucket's upper edge `le`. This is a standard approximation, documented in the function's docstring, and conservative in the direction of *overstating* latency rather than hiding a real regression. A precise reconstruction is not possible from histogram data alone; if exact per-request latency matters more than this approximation later, the fix is switching `sample-app` to a `Summary` with quantile objectives or exporting raw traces, not a verification-engine change.

## Real bugs found and fixed (found by actually running this against real Prometheus, not by reasoning about the code)
1. **Missing `curl` in `sample-app`'s Docker image.** Adding a `docker-compose` healthcheck to these containers for the first time silently failed forever (`python:3.11-slim` has no `curl`) — the container looked "unhealthy" with a perfectly working app underneath. Fixed by installing `curl` in the Dockerfile, matching every other service's pattern.
2. **Label collision**: the app's own `cohort` label on its Prometheus metrics collided with the scrape-config's external `cohort` label, causing Prometheus to rename the app's copy to `exported_cohort` — confusing and redundant. Fixed by removing the label from the app's own metric definitions entirely; the external scrape-config label is the sole source of truth (an app instance shouldn't need to self-report its own cohort for its own metrics — that's an infrastructure-level concern).
3. **`str.format()` vs. PromQL's own `{...}` syntax**: substituting the `{cohort}` placeholder with Python's `.format(cohort=cohort)` broke on every query, because PromQL's label-selector syntax (`{cohort="...",outcome="..."}`) is *also* curly braces, which `.format()` tries to parse as its own field spec (`ValueError: unexpected '{' in field name`). Fixed with plain `str.replace("{cohort}", cohort)` instead.
4. **Invalid PromQL from a `sum()`-wrapped `total_query`**: the business-metric's total-count query was written as `sum(payments_requests_total{cohort="{cohort}"})`, then wrapped in `increase(...[window])` by `fetch_counter_totals` — but `increase()` cannot be applied directly to an already-aggregated expression like that (needs subquery syntax `[window:step]`, not a bare range selector). Fixed by removing the redundant `sum()` — `fetch_counter_totals` already sums across every series `increase()` returns, so the raw un-aggregated selector was the correct query all along.
5. **Tested the wrong container**: after editing `pipeline-worker`'s code, the *first* end-to-end test actually ran the stale, pre-edit Docker image (numbers matched the old synthetic fallback's exact hardcoded ratios) — a reminder that `docker compose up -d --build <service>` is required after every backend edit, not just a container restart.
6. **Duplication discovered**: `sample-app/` (repo root) and `services/sample-app/` are two previously-identical copies of the same code with no single source of truth. Kept in sync for this phase (both instrumented identically); de-duplicating properly is a small follow-up, not done here since it's orthogonal to Phase 2's goal.

## Live proof (not just tests)
- A `HEALTHY` verdict driven entirely by real scraped data (`n_baseline=602`, real Fisher's-exact contingency table `[[607,3],[598,4]]` from real request outcomes) triggered a real autonomous promotion — OPA `PROMOTE_STEP`, a real `HTTPRoute` weight patch to 25% canary.
- Injecting **real** errors/latency into the running canary (`CANARY_INJECT_ERRORS=true`, not a synthetic parameter) produced a real `FAILED` verdict (composite score 64.29, just under the 65 threshold, driven by a genuinely significant Mann-Whitney/KS latency shift) which triggered a real autonomous rollback — OPA `ROLLBACK`, canary weight back to 0%.
- All through the unmodified "Trigger New Rollout" button — the entire real-telemetry wiring is invisible to the end user; only the honesty of the data underneath changed.

## Acceptance criteria
- [x] A live demo run's verdict is traceable to an actual PromQL query and an actual scraped value — demonstrated with real error injection driving a real `FAILED` verdict.
- [x] The existing test suite still passes unmodified: 68 (verification-engine, incl. 7 new prometheus_client tests) + 13 (adversarial) + 3 (policy-controller) + 3 (pipeline-worker) + 7 (explainability-service) + 4 (OPA) + 9 (frontend unit) — all green.
- [x] `docker compose up` still works with zero required configuration — Prometheus + both sample-app cohorts + the load generator all come up as part of the stack.

## Depends on
Nothing — done, independent of Phase 1. Phase 3 (Multi-Service Onboarding) should build its generated pipelines with a `prometheus` block by default (following this demo pipeline's pattern), not the synthetic fallback.
