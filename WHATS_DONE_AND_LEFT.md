# What is done and what is left (overall)

Snapshot: 2026-09-26. Detail lives in `PROJECT_STATUS.md` (full reference) and `BACKLOG.md` (incomplete items only).

## 1. The six infra-agent items (this session's batch)

| # | Item | Built | Tested | Live-verified | Left |
|---|---|---|---|---|---|
| 1 | Real cost calculation (AWS Price List, independent of the model) | Yes | 25 unit tests | Yes: real draft $46.72 (computed) vs $43.00 (model) | Nothing |
| 2 | Infra policy enforcement (`infra_guardrails.rego`) | Yes | OPA 39/39, gateway tests | Yes | Nothing |
| 3 | Cross-account provisioning (BYO-AWS via `sts:AssumeRole`) | Yes (backend + `AwsAccountPicker`) | 35 + 12 unit tests | Yes: real role, confused-deputy denial, least-privilege denials, real draft + change set through the assumed role | Nothing |
| 4 | Infra failure RCA | Yes (analyzer, worker events, gateway endpoint, UI card) | 12 + 6 + 5 unit tests | Yes, on a real failing stack (S3 name conflict) | Nothing |
| 5 | Blue-green cutover UI | Yes (`BlueGreenCutoverPanel`) | 12 unit tests | Yes: real blue-green run on AWS ECS; panel browser-verified through every phase | Rolled-back state only unit-tested |
| 6 | CloudWatch right-sizing | Yes | 22 unit tests | Yes, on a temporary Fargate service (torn down) | Nothing |

## 2. Platform overall

| Area | State |
|---|---|
| Five backend services + infra containers | Done, healthy under docker compose |
| Statistical verification engine, signed verdicts, OPA gating | Done, live-verified |
| Canary ramp and graduation, rollback (Kubernetes and AWS ECS) | Done, live-verified |
| Project workspaces, GitHub OAuth, autonomous push-triggered loop (two gates) | Done, live-verified |
| Auth hardening (bcrypt, JWT, refresh rotation, rate-limit, RBAC) | Done |
| Real telemetry (Prometheus, CloudWatch) | Done |
| AI infra generation, existing-vs-AI-created, prompt editing | Done, live-verified |
| ChatOps query interface | Done |
| Blue-green deploy mode (backend) | Code-complete; live verification still open (BACKLOG P0 1-6) |

## 3. Still open

| Priority | Item | Note |
|---|---|---|
| Now | Commit the fixes made during live verification (uncommitted) | |
| P0 | Blue-green end to end is DONE (live). Still open: post-cutover rollback drill (needs an app that answers 5xx), Dockerfile synthesis for Vite/Next/static, health-check defaults, live-URL semantics | BACKLOG P0 rows 2, 4, 5, 6 |
| P2 | `AWS_ALB_BASE_URL` is static and goes stale when the ALB is recreated; failed-validation pipelines are retried instead of dead-lettered | BACKLOG P2 7a/7b |
| P2 | Orphaned ALB listener rule if a project's `path_prefix` changes | Low severity |
| P3 | Build/Test/Deploy as dedicated routes; hosting the platform itself publicly (Phase 7); visual refresh (Phase 1b) | |
| P4 | TLS and secrets store; OpenTelemetry tracing; `tests/e2e/*.py`; MinIO (wire a use or drop); Celery migration; root AWS password rotation | Deferred or operational |

## 4. Test status

| Suite | Result |
|---|---|
| api-gateway | 342 passed |
| pipeline-worker | 280 passed, 5 skipped |
| explainability-service | 92 passed (needs `PYTHONPATH` with the repo root) |
| policy-controller | 132 passed |
| OPA | 39/39 |
| frontend vitest | 36 passed |
| `tsc -b` | clean |
