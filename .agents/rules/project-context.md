# Project context — always read before starting work

Full detail: `AGENTS.md` at the repo root (this file mirrors it, kept under Antigravity's
rules-file size cap) and, more completely, `CLAUDE.md`.

## What this is

**Smart AI DevOps & Continuous Delivery Platform** — CI/CD that verifies its own deployments.
A verification engine runs genuine statistical tests (SPRT, Mann-Whitney U, CUSUM/BOCPD,
Fisher's exact/χ², Isolation Forest) comparing a canary's live behavior against a baseline
cohort, signs the verdict (HMAC), and a policy controller only acts on a verdict it can prove
came from the verification engine, gated by OPA. Originally a university assignment
("Assignment 05" — see `MASTER_BUILD_SPEC.md`), now being extended into a real product (see
`docs/roadmap/00-ROADMAP.md`).

## Read before any non-trivial change

`CLAUDE.md` (full architecture + every trap already hit once), `PROJECT_STATUS.md` (current
done-vs-left, most trustworthy), `BACKLOG.md` (same, incomplete items only, priority-ordered).
Update status first, then remove the backlog row, whenever you finish and **live-verify** an
item — never mark something done from code existing alone.

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
