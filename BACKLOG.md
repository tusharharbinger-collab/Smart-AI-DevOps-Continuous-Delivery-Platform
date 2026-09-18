# Backlog — everything left, most important first

Derived from `PROJECT_STATUS.md` on 2026-09-16, with every ✅ Done item
stripped out. This is only what's incomplete — unbuilt features, partial
work, and real bugs/patches — ordered **most important/must-have first,
descending to lowest priority**. Update `PROJECT_STATUS.md` first when
something here gets done, then delete its row from here.

**Scope decision (2026-09-16): AWS ECS only, going forward.** Every
Kubernetes/Kind/EKS-specific item and every non-AWS cloud target has been
removed from this list — not deferred, removed, per an explicit choice to
focus exclusively on the AWS ECS path. Known, accepted tradeoff: EKS was
the only target satisfying the graded assignment's Kubernetes/Gateway API
rubric requirement (`MASTER_BUILD_SPEC.md` §5.1/§5.3) — see
`PROJECT_STATUS.md`'s "Deploy-target decision" for the full note. The
Kubernetes/Kind/EKS code itself was NOT deleted, just deprioritized — this
is a backlog-scope decision, not a codebase deletion.

Removed items, for the record: the shared `DeploymentTarget` interface
(existed only to unify ECS with Kubernetes/EKS — moot with one target),
in-cluster Prometheus + its pipeline-YAML wiring (Kubernetes-only),
`eksctl` teardown's orphaned-ELB bug, `_apply_http_route`'s 409-conflict
bug (Kubernetes `HTTPRoute`-specific), and Render/Vercel/Railway/Azure
targets.

---

## P0 — Live-verify the "Guaranteed Live Web App CI/CD" build (2026-09-17)

**Code-complete and unit-tested (432 backend tests + 11 OPA tests, zero
regressions; `tsc -b`/`vite build` clean) — nothing below is live-verified
yet.** Per this file's own convention, none of it moves to `PROJECT_STATUS.md`'s
✅ column until proven against the real `smartcd-platform` AWS account. See
`PROJECT_STATUS.md`'s 9.4 for the full per-item breakdown.

| # | Task | What it takes |
|---|---|---|
| 1 | **Live-verify blue-green end to end** | Onboard a real, genuinely zero-traffic repo with `deploy_mode: blue_green`, confirm the health-gated cutover (`HEALTH_GATED_CUTOVER` OPA rule, `wait_for_target_group_healthy`, `cutover_blue_green_ecs_weights`) actually reaches the live URL without any real traffic — the exact bug this was built to fix |
| 2 | **Live-verify post-cutover automatic rollback** | A real kill-and-recover drill: break the app after a successful cutover, confirm `rollback_blue_green_ecs_weights` actually reverts traffic and the pipeline reports FAILED, not COMPLETED |
| 3 | **Apply migration `0014`** to a real database (`deploy_mode`/`live_url_status`/`live_url_verified_at` on `projects`) |
| 4 | **Live-verify Dockerfile synthesis for real Vite/Next.js/static repos** | An actual `docker build` against each new template (`dockerfile_synthesis.py`'s spa/nextjs/static) — confirm the nginx SPA-fallback config and the multi-stage build actually produce a working image, not just correct-looking template text |
| 5 | **Live-verify health-check defaults + ALB matcher** | Onboard a real static/SPA project via the wizard, confirm the pre-filled `/` health-check path and the `200-399` target-group matcher actually keep ECS from killing a healthy task |
| 6 | **Live-verify live-URL verification** | Confirm `verify_live_url` actually catches the "healthy target group, 404 through the real URL" class of bug (ALB path-prefix trap) on a real deploy, and that `live_url_status` renders correctly in a real browser |
| 7 | **Blue-green UI cutover view** | Not started — health-check-in-progress/atomic-switch/old-service-termination as distinct visible phases; today the wizard shows deploy-mode + a post-hoc live-URL badge only, not a step-by-step cutover view |

## P1 — Right-sizing follow-through (last piece of Reports & Cost)

| # | Task | What it takes | Source |
|---|---|---|---|
| 5 | **Wire AI right-sizing to the now-real CloudWatch saturation data** | `cost_analyzer.py::compute_rightsizing_recommendation` and `cloudwatch_client.py::fetch_saturation_samples` (done + live-verified 2026-09-16) both exist and are both tested — nothing yet calls the first with the second's real output and persists `cost_analysis.rightsizing_rec`. AWS ECS only — no equivalent exists for Kubernetes projects, whose in-cluster-Prometheus telemetry work was removed from scope entirely under the AWS-ECS-only decision, not deferred | 9.1 |

## P2 — Real bugs / patches (undone, not hypothetical, AWS-relevant)

| # | Bug | Where |
|---|---|---|
| 7 | Orphaned ALB listener rule if a project's `path_prefix` changes between onboardings | AWS ECS path (low severity — not user-editable post-creation today) |

## P3 — Broader platform completeness

| # | Task | What it takes | Source |
|---|---|---|---|
| 8 | Build/Test/Deploy as **dedicated routes**, not wizard-step panels | Frontend routing split | 9.2 |
| 10 | Phase 7 — Local → Global platform deployment (hosting the *platform itself* publicly, not just projects it deploys) | Not started | Roadmap |
| 11 | Phase 1b — "Autonoma" Visual Refresh | In progress | Roadmap |

## P4 — Deferred / low urgency / operational

| # | Task | Note |
|---|---|---|
| 12 | TLS termination + migrating off plaintext `.env` to a real secrets store | Explicitly deferred to Phase 7 |
| 13 | Full OpenTelemetry tracing spans | Explicitly deferred, labeled stretch goal |
| 14 | `stage_logs` persistence | Explicitly deferred (Phase 8) |
| 15 | `tests/e2e/*.py` | Missing per assignment spec |
| 16 | `scripts/demo/*.sh` (`make demo-healthy`/`make demo-fail`) | Missing per assignment spec |
| 17 | MinIO — container runs, nothing writes to it | Either wire a real use or drop it |
| 18 | Celery migration for `pipeline-worker` | Spec names Celery; current sync+Redis-Streams implementation is functionally equivalent — low value to change |
| 19 | Root AWS account password rotation | Operational, your own action — flagged after being pasted in chat by mistake |

---

**Done (2026-09-16):** ECS Fargate cost tracking, the GitHub webhook
receiver, Gate 1 (build+test before any deploy attempt), the webhook
polling fallback, Reports & Cost UI with both AI integrations (digest
summary + gate-1 failure RCA), CloudWatch telemetry for AWS-deployed
projects (`cloudwatch_client.py`, wired through the first-verdict and
every-subsequent-step reverify paths — 3 real bugs found and fixed along
the way, 32 new tests, live-verified against the real `payments-aws` ECS
service), simplifying the onboarding wizard to AWS ECS only (dead
Kubernetes toggle removed from `NewProject.tsx`), and fixing Pause/
Resume/Emergency Rollback's disabled state — a genuinely bigger fix than
it first looked: `ProjectWorkspace.tsx` turned out to hold its own
separate, unsynced copy of these three buttons (`disabled={!selectedRunId}`,
ignoring status entirely) that's what actually renders — the already-fixed
`PipelineDashboard.tsx` copy is hidden via `hideRunControls` in this
layout. Also found and fixed a second real bug along the way: `GET
/api/v1/pipelines/runs/{id}`'s Postgres fallback read raw
`pipeline_executions.status` (always `'PENDING'`) instead of
`COALESCE(execution_state.status, pipeline_executions.status)` — live-
verified by clearing a real run's Redis cache and confirming the endpoint
now correctly returns `FAILED` instead of stale `PENDING`. All of the
above driven headlessly in a real browser against the real running stack,
not just code-reviewed.

**Also done (2026-09-16), found via a real end-to-end onboarding attempt
of a brand-new minimal project, not a hypothetical:**
- **Test commands are now genuinely optional and non-blocking.** The
  wizard used to default `test_command` to a hardcoded, Python-specific
  `"pytest tests/"` regardless of a project's actual language, and any
  test-command failure (wrong language, no matching tests) hard-failed
  the whole pipeline — even when the project's own Dockerfile already ran
  its real tests as a build layer. Fixed in `worker.py`, `build_preview.py`
  (Gate 1's dry run), `projects_router.py`'s YAML generator, and the
  wizard's default value — a missing command is now cleanly skipped, a
  failing one is a visible warning, never a blocker.
- **ECR repositories are now auto-created.** Unlike most registries, ECR
  never auto-creates a repository on first push — a project's very first
  build failed outright unless someone had already run
  `aws ecr create-repository` by hand, which nothing in the wizard ever
  told a user to do. `ensure_ecr_repository_exists` (idempotent) now runs
  automatically in the build stage whenever the declared image is a real
  ECR URI.
- **The wizard no longer silently guesses a broken container registry.**
  It used to auto-fill `container_image` as `registry.internal/{name}`
  regardless of deploy target — a placeholder host that only resolves
  inside the local dev network, so a real AWS ECS Fargate task could never
  pull it (this is exactly what caused a real live 503 this session). Now
  left blank with clear guidance, since no safe default exists without
  knowing the caller's own AWS account id.
- **The path-prefix requirement is now documented, not a silent trap.**
  AWS ALBs forward a project's full path prefix to its container — they
  cannot rewrite/strip it — so an app using exact route matching (not a
  catch-all or SPA-style fallback) will 404 through the live URL even
  though it works perfectly under `docker run`. This is a permanent
  characteristic of the shared-ALB design (see `PROJECT_STATUS.md`'s
  "Deploy-target decision"), not something fixable in the platform itself
  — the wizard's Networking section now says so explicitly.

All of the above driven headlessly in a real browser or against the real
running AWS account, not just code-reviewed. Full detail in
`PROJECT_STATUS.md`.

**Also done (2026-09-18):** the ChatOps query interface (the assignment's
own named bonus item — a grounded query interface over real comparison
data) — `POST /{project_id}/ask` (api-gateway) proxies to
`POST /chatops/ask` (explainability-service), which assembles real
run/verdict/audit evidence and answers via Groq with the same
hard-timeout + deterministic-fallback discipline as the existing RCA/
digest integrations; a `ChatOpsPanel` on the Pipeline View tab is the UI.
Read-only by construction — touches no pipeline/verification/actuation
code. Full detail in `PROJECT_STATUS.md` §9.7. Also fixed the same
session: a pipeline run rejected by the per-tenant concurrency lock used
to vanish as a ghost `PENDING` run the crash-recovery reconciler could
never find — it now writes a real terminal `FAILED` execution state with
an explicit reason.

Next up per this ordering: **#1 (Blue-green — OPA rule)**. Say the number
or name of whichever you actually want built first — this list is
priority-ranked, not a mandated sequence.
