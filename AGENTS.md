# AGENTS.md

Project-instruction entry point for AI coding tools (Antigravity, Cursor, Windsurf, Copilot,
etc.). This is a condensed pointer, not a replacement for the repo's own docs — **read
`CLAUDE.md` in full before making any non-trivial change**; it is longer than this file's
size budget allows and carries the complete, currently-accurate picture.

## What this is

**Smart AI DevOps & Continuous Delivery Platform** — CI/CD that verifies its own deployments.
A verification engine runs genuine statistical tests (SPRT, Mann-Whitney U, CUSUM/BOCPD,
Fisher's exact/χ², Isolation Forest) comparing a canary's live behavior against a baseline
cohort, signs the verdict (HMAC), and a policy controller only acts on a verdict it can prove
came from the verification engine, gated by OPA. Originally a university assignment
("Assignment 05" — see `MASTER_BUILD_SPEC.md`), now being extended into a real product (see
`docs/roadmap/00-ROADMAP.md`).

## Where to look for what

Read in roughly this order depending on the task:

| File | What it's for |
|---|---|
| `CLAUDE.md` | **Read this first for any non-trivial change.** Full architecture, all 8 key design invariants, and every non-obvious trap already hit and fixed once — don't reintroduce them. |
| `MASTER_BUILD_SPEC.md` | The original design spec + grading rubric — exact reference implementations, file-by-file. |
| `PROJECT_STATUS.md` | Current, actively-maintained done-vs-left state. More trustworthy than any narrative prose elsewhere. |
| `BACKLOG.md` | The same state, collapsed to only incomplete items, priority-ordered. |
| `README.md` / `ARCHITECTURE.md` | User-facing quick-start and one-page design-decision summary. |
| `SYSTEM_GUIDE.md` | Complete reference: every service, table, route, env var, what's real vs. not, troubleshooting. |
| `SETUP.md` | From-scratch bootstrap on a new machine (env vars, OAuth app registration, demo seeding). |
| `KNOWLEDGE_BASE.md` | Narrative + diagnostic trail for real bugs already found by actually running the system, plus an onboarding runbook. |

Update `PROJECT_STATUS.md` first, then remove the row from `BACKLOG.md`, whenever you finish
and **live-verify** a backlog item — don't mark something done from code existing alone.

## The 8 invariants — never violate these

1. **No static thresholds.** Every verification decision comes from a statistical test or a
   composite score derived from them — never a bare `if metric > X`.
2. **Metric routing is by `category`** (`error_rate`, `latency`, `saturation`,
   `business_metric`) — `engine.py`'s dispatcher routes purely on that field.
3. **`verification-engine` is structurally barred from touching Kubernetes.** No `kubernetes`
   dependency, no kubeconfig mount. Never add a k8s client import there.
4. **Verdicts are cryptographically signed** (HMAC-SHA256) before publishing; `policy-controller`
   must verify the signature (and a freshness window) before acting on one.
5. **Traffic shifting goes through the HTTPRoute (or, on AWS ECS, the ALB listener rule) —
   never the Deployment/replica count.** No pod restarts.
6. **Multi-tenancy is enforced via Postgres RLS**, keyed on `tenant_id`, set per-request via
   `SET LOCAL`/`set_config`. Never bypass RLS with a superuser connection for app queries.
7. **Sample-size floor is N ≥ 100**, enforced independently in Python and in OPA (defense in
   depth) — don't lower one without the other.
8. **A `Project` wraps a `Pipeline`; it never replaces the pipeline/run model.** Never add a
   separate "project run" table or duplicate verdict/audit logic for projects.

## Deploy target

**AWS ECS Fargate is the sole build focus for new deploy-target work** (explicit 2026-09-16
scope decision, see `PROJECT_STATUS.md`'s "Deploy-target decision"). Kubernetes/Kind/EKS code
still exists and works but is deprioritized — check a project's `deploy_target` field
(`kubernetes` vs. `aws_ecs`) rather than assuming.

## Commands

```
docker compose up -d --build      # full stack
make test-all                      # test-stats + test-engine + test-guardrails + test-opa
cd services/<name> && python -m pytest tests/ -v   # per-service tests
opa test policies/ -v              # OPA policy tests (bin/opa.exe on Windows)
```

On Windows, this repo's folder name contains an `&`, which breaks npm's `.cmd` shims under
cmd.exe-backed shells — call the underlying script directly:
`node node_modules/vite/bin/vite.js` (dev server), `node node_modules/.bin/tsc -b` (type-check).

## Before claiming something works

This project's established discipline, carried across every session: verify against the real
running stack (containers, real DB queries, real AWS API calls, real curl against a live URL)
before declaring a fix or feature done. Don't infer success from code existing or reading
correct — run it.
