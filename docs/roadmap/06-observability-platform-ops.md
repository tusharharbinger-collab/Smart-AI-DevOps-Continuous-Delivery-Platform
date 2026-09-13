# Phase 6 — Platform Observability

**Status: done, verified live end-to-end (a real cross-service trace_id query, a real Grafana dashboard backed by real Prometheus data, and a real alert fired by actually stopping a container).**

## Goal
When the platform itself misbehaves, we find out from a dashboard/alert — not from a confused user report.

## Why now
Built once Phase 5's task-queue/worker model existed — there's something concrete to instrument (queue depth, task failure rate) rather than generic request counts.

## What shipped

### 6.1 — Centralized logging
- [x] **Loki + Promtail** added to `docker-compose.yml` (`monitoring/loki-config.yml`, `monitoring/promtail-config.yml`) — Promtail discovers every container via the Docker socket (picks up Phase 5's scaled replicas automatically) and ships stdout to Loki; every log line is already JSON (`shared/logging_config.py`), so Promtail's pipeline stage parses `service`/`level` into queryable Loki labels without adding `trace_id` as a label (would blow up cardinality — it's filtered via `| json | trace_id="..."` instead).
- [x] **`trace_id` now actually propagates across all 4 services a rollout touches** — previously only ever bound in api-gateway's own middleware. Threaded through: the `pipeline:start` stream payload → `bind_request_context()` in pipeline-worker → an `X-Trace-Id` header on the `/verify` call → bound in verification-engine → included in the `stream:verdicts` entry → bound in policy-controller. **Live-verified**: one triggered rollout with a known `X-Trace-Id`, and a single Loki query (`{service=~".+"} | json | trace_id="..."`) returned every log line from api-gateway, pipeline-worker, verification-engine, and policy-controller for that one run, in order — exactly the acceptance criterion.

### 6.2 — Platform metrics
- [x] All 5 backend services now expose `/metrics` (Prometheus exposition format) via `prometheus-fastapi-instrumentator` — RED metrics (request rate/latency histograms/error rate per endpoint) with zero hand-rolled middleware.
- [x] Queue depth + pending count gauges (`pipeline_queue_depth`/`pipeline_queue_pending` on pipeline-worker, `verdict_queue_depth`/`verdict_queue_pending` on policy-controller) — built on Phase 5's Streams (`shared/redis_streams.py`'s new `stream_length`/`pending_count` helpers), updated on a 10s loop.
- [x] `verification_verdicts_total{status}` counter on verification-engine — verdict outcome counts, incremented once per `/verify` call.
- [x] `monitoring/prometheus.yml` scrapes all 5 services (new `platform-services` job) alongside the sample-app pair from Phase 2.
- [x] A provisioned Grafana dashboard (`monitoring/grafana/provisioning/dashboards/platform-health.json`, auto-loaded, zero manual setup) — 7 panels: pipeline/verdict queue depth+pending, api-gateway latency p50/p95/p99, request+error rate by service, verdict outcome distribution (pie, 24h), verdict rate by outcome, and service readiness (`up`). **Live-verified**: queried Grafana's own datasource-proxy API for each panel's exact PromQL and confirmed real, non-empty results.

### 6.3 — Alerting on the platform itself
- [x] `services/policy-controller/src/platform_health_monitor.py` — polls every service's `/readyz` every 20s; a service unready for more than 120s (both configurable) fires a `PLATFORM_UNHEALTHY` alert through the *existing* `alert_dispatcher.send_alert` Slack-webhook path (no second alerting mechanism, per the roadmap's own instruction) — distinct from `alert_rollback`/`alert_approval_required`, which are about decisions made *about a verified service*. Fires exactly once per outage (not every poll) and sends `PLATFORM_RECOVERED` once the service comes back.
- [x] **Live-verified with a real container stop**: stopped `verification-engine`, confirmed no alert fires before the threshold, confirmed `platform_service_unhealthy_alert_fired` (with the correctly-attributed `checked_service` field — see bug #2 below) after it, restarted the container, confirmed `PLATFORM_RECOVERED` fired immediately.
- [ ] **Honest scope note**: this loop runs inside policy-controller, so it cannot detect policy-controller's *own* total outage — documented in the module's own docstring. The Grafana `up{job="platform-services"}` panel (6.2) covers that case instead, since Prometheus polls from outside every process. A fully separate watchdog would close this gap but would also be "a second alerting path," which the roadmap explicitly said not to build.

### 6.4 — Distributed tracing — **deferred, per the roadmap's own "stretch goal" label**
Not built. All three of this phase's actual acceptance criteria are met by 6.1's `trace_id` correlation without it: pulling every log line for one run, a dashboard with queue depth/latency/verdict distribution, and an alert on a stopped service. Full OpenTelemetry spans + Tempo would add real value specifically for *within-request latency breakdowns* (which service ate the time), but that's a different question than anything this phase's acceptance criteria actually ask, and instrumenting 5 services' HTTP calls AND Redis Streams messages (a non-standard trace-context carrier, needing manual W3C traceparent injection/extraction) is substantial new scope. Revisit if/when latency debugging specifically becomes a real pain point.

## Real bugs found and fixed (found by actually running this, not by reasoning about the code)
1. **`asyncio.to_thread` context propagation confirmed correct, but needed explicit verification** — `bind_request_context()` called before `asyncio.to_thread(orchestrator.start_pipeline, ...)` in pipeline-worker only reaches the worker thread's log lines because `asyncio.to_thread` copies the calling coroutine's `contextvars.Context` into the new thread (`ctx.run(...)`); this is real Python behavior, not something this codebase adds, but it was worth confirming live (via the trace_id test) rather than assuming.
2. **Structured-logging field collision**: `platform_health_monitor.py`'s log calls passed `service=name` (the service being *checked*) — but `shared/logging_config.py`'s `_add_service_name` processor unconditionally overwrites the `service` key with the *current process's own* name on every log line. The alert message text was unaffected (built from an f-string, not the log field), but the structured log field silently always said `"policy-controller"` regardless of which service was actually unhealthy — caught by reading the live log output, not by inspection. Fixed by renaming the field to `checked_service`.

## Live proof (not just tests)
- One rollout, triggered with `X-Trace-Id: test-trace-<ts>`, produced log lines in api-gateway, pipeline-worker, verification-engine, and policy-controller all carrying that exact id — retrieved in one Loki query.
- Grafana's `pipeline_queue_depth` panel returned a real value (44) sourced from a live `XLEN` on the actual Redis stream.
- Stopping `verification-engine` for over the configured threshold produced a real `PLATFORM_UNHEALTHY` alert (visible in logs; `SLACK_WEBHOOK_URL` isn't configured in this dev environment, so `send_alert` logged `alert_skipped_no_webhook` rather than posting — the dispatch logic itself, exercised identically to the existing rollback-alert tests, is what's being verified here, not Slack delivery); restarting it produced a real `PLATFORM_RECOVERED` alert.

## Acceptance criteria
- [x] Given a `pipeline_run_id`, every log line across all services for that run is retrievable in one query, via `trace_id` correlation.
- [x] A Grafana dashboard shows current task queue depth, request latency percentiles, and verdict outcome distribution over the last 24 hours.
- [x] Deliberately stopping the `verification-engine` container triggers a platform-unhealthiness alert within a defined SLA (configurable; 120s default) — distinct from any rollback-decision alert.

## Depends on
Phase 5 (Reliability & Scale) — the queue-depth metrics this phase exposes are built directly on Phase 5's `shared/redis_streams.py`.
