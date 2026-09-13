# Push Plan — Staged Deployment to GitHub

This file tracks the incremental push schedule for this repository. Each
chunk is pushed as its own commit, roughly one hour apart, so the commit
history reflects a real, step-wise build rather than one bulk upload.

**Rule:** before any chunk is pushed, if any files in it were modified since
this plan was written, that is noted in the "Notes" column below *before*
the push happens — this file is always updated first, then the commit is
made and pushed.

Remote: `https://github.com/tusharharbinger-collab/Smart-AI-DevOps-Continuous-Delivery-Platform.git`

## Status legend
- ⬜ Pending — not yet pushed
- ✅ Pushed — commit made and pushed to `origin`

## Chunks

| # | Name | Contents | Status | Pushed at (UTC) | Commit | Notes |
|---|------|----------|--------|------------------|--------|-------|
| 1 | Project scaffolding & docs | `.gitignore`, `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `MASTER_BUILD_SPEC.md`, `Makefile`, `alembic.ini`, `.env.example`, `docs/`, `Assignment 05 - ...docx`, `PUSH_PLAN.md` | ✅ | 2026-09-13 10:02 | `7239107` | Merged GitHub's auto-generated placeholder "Initial commit" (1-line README) first; our real README superseded it. |
| 2 | Shared infra & DB schema | `shared/`, `migrations/` | ✅ | 2026-09-13 11:02 | `596ce33` | No drift since chunk 1 — files unchanged from original plan. |
| 3 | Verification Engine | `services/verification-engine/`, `GAP_ANALYSIS_HARDENING_SPEC.md` | ✅ | 2026-09-13 12:44 | (pending) | Drift since chunk 2: a Phase 1 hardening pass added `src/promql_validator.py` (validates user-supplied PromQL, rejects syntax errors/scalar results, 5s timeout) + `tests/test_promql_sanitizer.py`, wired into `main.py`'s `/verify`. `GAP_ANALYSIS_HARDENING_SPEC.md` (new root doc covering this whole hardening pass, chunks 3-6) is riding along with this chunk since it's the first of them. |
| 4 | Policy Controller + OPA policies | `services/policy-controller/`, `policies/` | ⬜ | | | Drift since chunk 2: same hardening pass added `test_rollback_actuation.py`, rewrote `alert_dispatcher.py` (added generic-webhook + email channels alongside Slack) with `test_alert_dispatcher.py` expanded accordingly. |
| 5 | Pipeline Worker | `services/pipeline-worker/` | ⬜ | | | Drift since chunk 2: same hardening pass added `src/schemas.py` (pipeline spec validation), `src/preflight.py` (image pre-flight check), `src/tasks/git_clone.py` (build-stage git-clone-by-URL), namespace-isolation changes to `src/k8s/onboarding.py`, a `docker` SDK fix to `build_task.py`/`preflight.py` (this container's base image has no `docker` CLI, only the daemon — real bug found live), and matching new test files. `Dockerfile` also changed (added `git`, kept `docker.io`). |
| 6 | API Gateway | `services/api-gateway/` | ⬜ | | | Drift since chunk 2: 4 real cross-tenant security leaks found and fixed (`pipeline_router.py`, `verification_router.py`, `logs_router.py`, `reports_router.py` — see `tests/adversarial/test_tenant_isolation.py` in chunk 11), plus per-tenant namespace derivation in `services_router.py`. |
| 7 | Explainability Service | `services/explainability-service/` | ⬜ | | | Drift since chunk 2: fixed a real bug where the digest endpoint 500'd for every tenant, always (RLS tenant context was never set on this service's own DB session) — `src/db.py` + `main.py`. |
| 8 | Kubernetes & sample app | `k8s/`, `sample-app/`, `pipelines/` | ⬜ | | | |
| 9 | Frontend | `frontend/` | ⬜ | | | Drift since chunk 2: `PipelineDAG.tsx` got a Failed (red) state + click-to-filter-logs; canary ramp chart now also shown in `VerificationInspector.tsx`. `tsc -b`/`vite build`/vitest/Playwright all re-verified clean after this. |
| 10 | Observability & scripts | `monitoring/`, `scripts/`, `bin/` | ⬜ | | | |
| 11 | Tests & compose wiring | `tests/`, `docker-compose.yml`, `docker-compose.scale-test.yml` | ⬜ | | | Drift since chunk 2: `tests/adversarial/test_tenant_isolation.py` added (13 tests covering chunk 6's security fixes). `docker-compose.yml` gained a `/var/run/docker.sock` mount on `pipeline-worker` (it never had one — see chunk 5's note). |

## Excluded from all pushes
- `.env` (real secrets — already gitignored)
- `.claude/` (local Claude Code session data — added to `.gitignore`)
- `.pytest_cache/`, `__pycache__/`, `node_modules/`, `frontend/dist/` (already gitignored)

## Log
- 2026-09-13 — Plan created. Remote added as `origin`. Awaiting working push
  credentials on this machine before chunk 1 goes out.
- 2026-09-13 10:02 — Git Credential Manager authenticated as `tusharharbinger-collab`.
  Chunk 1 committed (`916564c`) and merged with GitHub's placeholder initial
  commit (`7239107`), then pushed to `origin/main`. Chunk 2 scheduled ~1h out.
- 2026-09-13 11:02 — Chunk 2 (`shared/`, `migrations/`) pushed to `origin/main`.
  Chunk 3 scheduled ~1h out.
- 2026-09-13 12:44 — Substantial drift detected before chunk 3: a full Phase 1-4
  hardening pass (input validation, tenant-isolation security fixes, registry
  pre-flight, git-clone-by-URL, per-tenant k8s namespaces, alert channels,
  frontend polish) landed across chunks 3-7, 9, and 11 since chunk 2 was
  pushed — each affected row's Notes column above records exactly what.
  Chunk 3 (`services/verification-engine/` + the new `GAP_ANALYSIS_HARDENING_SPEC.md`)
  pushed to `origin/main`. Chunk 4 scheduled ~1h out.
