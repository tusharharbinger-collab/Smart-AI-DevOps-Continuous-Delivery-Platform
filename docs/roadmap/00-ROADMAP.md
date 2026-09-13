# Production Upgrade Roadmap

This is the master index for turning this project from "a working assignment build" into something that looks, feels, and operates like a real product. Each phase below is its own file with concrete deliverables, tech choices, and an acceptance checklist. Read this file first for the overall shape and ordering logic; read a phase file when you're ready to start that phase.

## Why this order

Phases are ordered by **what the next phase would otherwise be built on top of a lie**. There's no point polishing a UI that shows fabricated verdicts, and no point generalizing onboarding for services whose traffic-control code only knows about one hardcoded route.

```
Phase 1: Frontend Overhaul          ─┐
                                      ├─ Both can start immediately, independent of each other.
Phase 2: Real Telemetry             ─┘  (Phase 1 is UI/UX; Phase 2 is data-truth. Neither blocks the other.)
        │
        ▼
Phase 3: Multi-Service Onboarding     ← needs Phase 2's real metrics to be meaningful for a NEW service,
        │                                not just the one demo service
        ▼
Phase 4: Security Hardening           ← needs Phase 3's onboarding flow to know what it's protecting
        │                                (per-service permissions, not just per-tenant)
        ▼
Phase 5: Reliability & Scale          ← not strictly blocked on Phase 3 (a 2nd pipeline-worker
        │                                replica would double-process jobs even with one service
        │                                today), but multi-service testing needs Phase 3 to exist
        ▼
Phase 6: Platform Observability       ← needs Phase 5's task queue/worker model to have something
        │                                meaningful to instrument
        ▼
Phase 7: Deployment (Local → Global)  ← last, because deploying an unfinished product just moves
                                          the unfinished product somewhere else
```

**Decided:** Phase 1 (Frontend Overhaul) and Phase 2 (Real Telemetry) run in parallel — they don't touch the same files and don't depend on each other. Phase 1 is the one explicitly prioritized ("should feel like a real world production-ready web app").

## Operating principle: full roadmap, no phase skipped, test-gated

**All 7 phases are in scope — nothing here is "someday, maybe."** The explicit instruction is to build the whole thing, phase by phase, and **verify each phase actually works before starting the next one** — not a big-bang effort across all 7 at once. Concretely, that means for every phase:
1. Work through its deliverable checklist.
2. Run its acceptance criteria for real (not "the code looks right" — actually run the test, actually click through the flow, actually check the log output) before checking it off as done.
3. Only then move to the next phase in the order above.

If a phase's acceptance criteria can't be verified yet (e.g., a dependency on a later phase's infrastructure), that's a signal the phase ordering needs revisiting — flag it rather than checking the box anyway.

## Phase summary

| # | Phase | Status | Core question it answers | File |
|---|---|---|---|---|
| 1 | Frontend Overhaul | ✅ Done | Does this look and feel like a real product? | [01-frontend-overhaul.md](01-frontend-overhaul.md) |
| 1b | "Autonoma" Visual Refresh | 🚧 In progress | Does it match the target visual design language, on real data? | [01b-autonoma-visual-refresh.md](01b-autonoma-visual-refresh.md) |
| 2 | Real Telemetry | ✅ Done | Is the statistics engine running on real data? | [02-real-telemetry.md](02-real-telemetry.md) |
| 3 | Multi-Service Onboarding | ✅ Done | Can a second, different team/service actually use this? | [03-multi-service-onboarding.md](03-multi-service-onboarding.md) |
| 4 | Security Hardening | ✅ Mostly done (TLS/secrets-store deferred to Phase 7) | Does it survive a hostile or careless user? | [04-security-hardening.md](04-security-hardening.md) |
| 5 | Reliability & Scale | ✅ Done | Does it survive real, concurrent, 24/7 use? | [05-reliability-scale.md](05-reliability-scale.md) |
| 6 | Platform Observability | ✅ Done (tracing spans deferred, per its own "stretch goal" label) | Can *we* tell when the platform itself is unhealthy? | [06-observability-platform-ops.md](06-observability-platform-ops.md) |
| 7 | Deployment (Local → Global) | Not started | Can this run somewhere other than one developer's machine? | [07-deployment-plan.md](07-deployment-plan.md) |
| 8 | Project Workspaces & GitHub Delivery | ✅ Done (stage_logs persistence deferred) | Can a user connect their own repo and get an isolated, project-scoped workspace? | [08-project-workspaces.md](08-project-workspaces.md) |

## What's already real (don't re-litigate these)

Established and verified in earlier work — treat as a foundation, not something to redo:
- Genuine statistical verification (SPRT, Mann-Whitney, KS, CUSUM, BOCPD, Fisher's/χ², Isolation Forest), correctly routed by metric category.
- Cryptographically signed verdicts (HMAC) + OPA policy guardrails, adversarially tested (forged verdict, freeze-window bypass, micro-sample attack all confirmed blocked).
- A real Kubernetes target: Kind + Envoy Gateway + real `HTTPRoute` traffic-splitting, verified with actual HTTP traffic and a real autonomous rollback (canary scaled to 0, zero baseline restarts).
- Real authentication: bcrypt-hashed passwords, HS256-signed JWTs, signature actually verified (the old forgeable-token hole is closed).
- Postgres Row-Level Security for multi-tenancy, verified isolating two different tenants over real HTTP requests.
- **Phase 1 (Frontend Overhaul) is done** — see [01-frontend-overhaul.md](01-frontend-overhaul.md) for the full breakdown. Real routing, a real component/design system, real charts from actual evidence, dark mode, and a tested golden path (9 unit + 6 e2e tests, all passing against the real running stack) — not a mockup.
- **Phase 2 (Real Telemetry) is done** — see [02-real-telemetry.md](02-real-telemetry.md). Real Prometheus scraping real sample-app traffic; verified live with real error injection driving a real `FAILED` verdict and a real autonomous Kubernetes rollback (and, separately, a real `HEALTHY` verdict driving a real autonomous promotion) — the synthetic generator is now an explicit, logged fallback, not the default.
- **Phase 3 (Multi-Service Onboarding) is done** — see [03-multi-service-onboarding.md](03-multi-service-onboarding.md). A second, real service (`checkout-service`) onboarded end-to-end through `POST /api/v1/services` (and, separately, through the UI alone via Playwright) — real generated Deployments/Services/HTTPRoute, a real isolated canary weight shift independent of the original `payments-pipeline`'s own weight. Actuation is now parameterized per-pipeline (no more hardcoded route/namespace/deployment constants in policy-controller).
- **Phase 4 (Security Hardening) is mostly done** — see [04-security-hardening.md](04-security-hardening.md). Role-based authorization now actually enforced (not just a signed-but-unchecked JWT claim), login rate-limiting + short-lived access tokens + revocable refresh tokens + a queryable auth audit trail all live-verified, `VERDICT_SIGNING_KEY` rotation support added, and a real (if non-obvious) network-isolation finding investigated and permanently guarded by tests. TLS termination and full secrets-store migration are explicitly deferred to Phase 7 (deployment-target-dependent — see that file's scope note).
- **Phase 5 (Reliability & Scale) is done** — see [05-reliability-scale.md](05-reliability-scale.md). Redis pub/sub replaced with Streams consumer groups for both the pipeline-trigger and verdict queues — live-verified with 3 replicas of each service processing 10 concurrent rollouts with zero duplicates and zero drops. Crash recovery is real (pipeline-worker now has a durable Postgres connection it never actually had before; a killed-mid-rollout pipeline genuinely resumes, verified by test). Found and fixed two real "fails unsafe" bugs by actually killing OPA and Redis: OPA-unreachable now fails open for autonomous rollback (never for promotion), and a Redis outage no longer permanently kills policy-controller's verdict consumer.
- **Phase 6 (Platform Observability) is done** — see [06-observability-platform-ops.md](06-observability-platform-ops.md). Loki+Promtail+Grafana added alongside Phase 2's Prometheus; `trace_id` now genuinely propagates across all 4 services one rollout touches (verified: one Loki query pulls every log line for one run across every service). All 5 services expose real RED metrics + queue-depth gauges + a verdict-outcome counter; a provisioned Grafana dashboard queries real data. A platform self-health monitor fires a real Slack-webhook-path alert when a service's `/readyz` fails for over 2 minutes — verified by actually stopping a container. Full OpenTelemetry tracing (6.4) is explicitly deferred, per its own "stretch goal" label in the roadmap.

## How to use these files

Each phase file has the same shape:
- **Goal** — one sentence, the thing that becomes true when this phase is done.
- **Why now** — why this phase, in this position in the order.
- **Deliverables** — a checklist of concrete features/changes.
- **Tech choices** — specific libraries/tools recommended, with the reasoning (not "use X because it's popular").
- **Acceptance criteria** — how we'll know it's actually done, not just "code exists."
- **Depends on** — which earlier phases must be done first, and why.

Update the checklists in place as work completes — these files are meant to be living trackers, not a one-time spec.
