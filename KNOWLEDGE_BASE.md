# Knowledge Base — Smart AI DevOps & Continuous Delivery Platform

**Last updated: 2026-09-16.** This is the single consolidated reference for
everything learned about this platform through real building, testing, and
live debugging — tech stack, architecture, every Docker image in use, what's
done vs. left, and (most valuable for next time) every real bug found this
session and exactly how it was diagnosed and fixed. Read this before
onboarding a new project or debugging a live failure; it exists so the next
person (or the next session) doesn't have to rediscover any of this the hard
way.

**Related docs, and when to use them instead of this one:**
- `PROJECT_STATUS.md` — the authoritative, actively-maintained done/left
  tracker. This file summarizes it; that file is the source of truth for
  current status.
- `BACKLOG.md` — the same information, priority-ordered, stripped to only
  what's incomplete.
- `CLAUDE.md` — contributor-facing architecture notes and invariants, aimed
  at an AI/human writing code in this repo.
- `SYSTEM_GUIDE.md` — complete technical reference (every route, every env
  var, every table).
- `SETUP.md` — from-scratch clone-and-run instructions.
- `MASTER_BUILD_SPEC.md` — the original assignment spec this was built
  against.

This file's job is different: it's the **narrative + troubleshooting**
layer — what actually happened, what actually broke, and how to reason
about a new failure quickly.

---

## 1. What this platform is

CI/CD that verifies its own deployments. A user connects a GitHub repo;
the platform builds it, deploys a canary alongside the live baseline,
compares the canary's real telemetry against the baseline using genuine
statistical tests (never a static threshold), and autonomously promotes or
rolls back — cryptographically signed, policy-gated, fully audited. After
the first human-confirmed deployment, every subsequent `git push` triggers
the entire build → test → verify → promote/rollback cycle with no human
involved, unless it isn't safe, in which case it blocks and explains why.

**Deploy target: AWS ECS Fargate only**, as of an explicit 2026-09-16
scope decision. Kubernetes/Kind/EKS code still exists and still works, but
new onboarding no longer offers it — see §7 for the full reasoning and
tradeoff.

---

## 2. Tech stack

**Backend — 5 independent Python services (FastAPI, containerized):**

| Service | Role | Key libraries |
|---|---|---|
| `api-gateway` | External REST/WebSocket/SSE API, auth, RLS-scoped Postgres sessions | SQLAlchemy 2.0 (async) + asyncpg, Alembic, PyJWT, bcrypt, sse-starlette |
| `pipeline-worker` | Build/test/deploy orchestration, DAG execution | `docker` SDK (docker-py), `kubernetes` client, `boto3`, `networkx` |
| `verification-engine` | Statistical canary verification (isolated, no k8s access) | NumPy, SciPy, scikit-learn, `boto3` (CloudWatch) |
| `policy-controller` | OPA policy evaluation + actuation (traffic shifting, rollback) | `kubernetes` client, `boto3` (ECS/ALB) |
| `explainability-service` | AI-grounded RCA and digest summaries | `httpx` → Groq API directly (no SDK) |

All 5: FastAPI 0.111, Pydantic 2.7, `structlog` (JSON logs), Prometheus
instrumentation, Redis 5.0 (Streams + consumer groups for inter-service
messaging).

**Frontend:** React 18 + TypeScript 5.5 + Vite 5, React Router 7, TanStack
Query 5, Zustand, Tailwind + shadcn/ui (Radix primitives), Recharts,
CodeMirror, Playwright (e2e) + Vitest (unit).

**Statistics (hand-implemented on NumPy, not a stats library):** Wald SPRT
(error rate), Mann-Whitney U + Kolmogorov-Smirnov (latency), CUSUM + BOCPD
(saturation), Fisher's exact/Chi-square (business metrics), Isolation
Forest (cross-metric anomaly).

**Policy engine:** Open Policy Agent 0.68 (Rego) — the single gate every
actuation passes through.

**Data/infra:** Postgres 16 (Row-Level Security multi-tenancy), Redis 7
(Streams, cache), MinIO (S3-compatible, provisioned but unused).

**Observability:** Prometheus 2.55, Grafana 11.2, Loki 2.9 + Promtail.

**AI:** Groq (`GROQ_MODEL`, defaults to `openai/gpt-oss-120b`) — RCA and
digest generation, always with a deterministic fallback if the call fails
or times out.

**AWS integration:** `boto3` — ECS (Fargate), ECR, ELBv2 (ALB/target
groups), CloudWatch (`GetMetricData`), STS-adjacent IAM (task execution
role).

**Auth:** bcrypt password hashing, HS256 JWT (15-min access + 7-day
revocable refresh token in Redis), GitHub OAuth 2.0 for repo connection.

---

## 3. Docker images in use

Run `docker compose config --services` for the authoritative list. As of
this writing, 17 services are defined; the always-needed core (Postgres,
Redis, OPA, the 5 backend services, frontend, sample-app pair) is what's
typically kept running — Loki/Promtail/Grafana/MinIO/load-generator are
defined but optional for day-to-day work.

**Custom-built images (this repo's own Dockerfiles):**

| Service | Base image | Purpose |
|---|---|---|
| `api-gateway` | `python:3.12-slim` | External API |
| `pipeline-worker` | `python:3.12-slim` | Build/deploy orchestration (also needs `docker.io` CLI + `git`, installed via apt) |
| `verification-engine` | `python:3.11-slim` | Statistics — deliberately the ONE service with no `kubernetes`/`boto3`-to-AWS-mutation capability beyond read-only CloudWatch |
| `policy-controller` | `python:3.12-slim` | OPA + actuation |
| `explainability-service` | `python:3.12-slim` | AI RCA/digests |
| `frontend` | `node:20-slim` | Runs `npm run dev` inside the container — **not a production build**. Source is `COPY`'d at image build time, not volume-mounted, so a code change needs `docker compose build frontend && docker compose up -d frontend` to actually show up. This tripped up live verification more than once this session — see §8. |
| `sample-app-baseline` / `sample-app-canary` | `python:3.11-slim` | Demo Kind-target service pair (v1.0.0 / v1.1.0) |

**Third-party infra images (pulled, not built):**

| Service | Image |
|---|---|
| `postgres` | `postgres:16-alpine` |
| `redis` | `redis:7-alpine` |
| `opa` | `openpolicyagent/opa:0.68.0` |
| `minio` | `quay.io/minio/minio:latest` |
| `prometheus` | `prom/prometheus:v2.55.1` |
| `loki` | `grafana/loki:2.9.9` |
| `promtail` | `grafana/promtail:2.9.9` |
| `grafana` | `grafana/grafana:11.2.0` |
| `load-generator` | `alpine/curl:8.9.1` |

**Ports (host → container), for the core services:**

| Service | Port |
|---|---|
| frontend | 3000 |
| api-gateway | 8000 |
| pipeline-worker | 8001 |
| verification-engine | 8002 |
| policy-controller | 8003 |
| explainability-service | 8004 |
| postgres | 5432 |
| redis | 6379 |
| opa | 8181 |
| prometheus | 9090 |
| sample-app-baseline / canary | 8081 / 8082 |
| grafana | 3001 (maps to container's 3000) |
| loki | 3100 |
| minio | 9000, 9001 |

**Note on this machine specifically:** it also hosts several *unrelated*
active projects with their own Docker containers (`datahub-*`,
`data-engineering-lab-*`, `airflowdocker-*`) — visible in `docker ps -a`
but not part of this platform. Never assume every running container on
this host belongs to this repo; always filter by the
`smart-ai-devops-continuous-delivery-platform-*` name prefix or check
`docker compose ps` from this repo's directory.

---

## 4. Architecture

```
Trigger (human, or autonomous git push)
        │
        ▼
   api-gateway  ───(Redis Streams)──▶  pipeline-worker
                                            │  build → test → deploy → canary_loop
                                            ▼ (HTTP)
                                   verification-engine
                                   (runs the real statistical tests,
                                    signs the verdict HMAC-SHA256)
                                            │ (Redis Streams, signed)
                                            ▼
                                   policy-controller
                                   (verifies signature → asks OPA →
                                    actuates traffic shift or rollback,
                                    or blocks)
                                            │
                                            ▼
                                   explainability-service
                                   (grounded RCA / digest via Groq,
                                    always with a fallback)
```

Every service is independently containerized, health-checked
(`/healthz` + `/readyz`), and JSON-logs via a shared module
(`shared/logging_config.py`).

**Two real deploy targets exist** — AWS ECS Fargate (current focus) and
Kubernetes via Kind + Envoy Gateway (still real, deprioritized from
onboarding). Both implement the same principle: traffic shifting happens
at the routing layer (ALB listener-rule weighted target groups for ECS,
Gateway API `HTTPRoute` weighted `backendRefs` for Kubernetes) — **never**
by changing replica counts, so promotion/rollback never restarts pods or
causes a blip.

**AWS ECS specifics (the live target):**
- One **shared** ALB (`smartcd-platform-alb`) and one shared ECS cluster
  (`smartcd-platform`) serve **every** onboarded project — not one ALB
  per project, to avoid per-project AWS cost.
- Each project gets its own target group per cohort:
  `{service_name}-baseline` / `{service_name}-canary`, truncated to AWS's
  32-character target-group-name limit.
- Each project gets its own **path prefix** on the shared ALB (e.g.
  `/api/v1/checkout`) instead of its own domain/listener. **The ALB
  forwards the full, unstripped path to the container — it cannot rewrite
  or strip a prefix.** This is a permanent AWS limitation, not a platform
  bug, and it means every onboarded app's own code must tolerate receiving
  requests at `<its prefix>/...` rather than assuming it owns `/`. See §8
  for the real incident this caused and the fix pattern.
- Real AWS account used this session: **236087863083**, region
  **us-east-1**. ALB DNS:
  `smartcd-platform-alb-1806553190.us-east-1.elb.amazonaws.com`.

**Autonomous update loop (git push → live, no human):**
1. GitHub webhook (`POST /api/v1/webhooks/github`, HMAC-verified) — or, if
   that can't reach this environment (see §8), a 15-minute polling
   fallback that diffs real GitHub branch HEAD against the last commit
   checked, using an outbound-only GitHub API call (works even with no
   public URL, since polling is the platform calling out, not GitHub
   calling in).
2. **Gate 1** — a real build+test dry run (`build_preview.py`), queued via
   `stream:gate1:check`. Only `passed=True` triggers a real rollout.
3. **Gate 2** — the existing verification/OPA/actuation chain, unmodified,
   reached the same way whether triggered by a human or autonomously.

---

## 5. What's complete

Full detail lives in `PROJECT_STATUS.md`. Headline summary:

- Core platform: 5 services, real statistical verification, signed
  verdicts, OPA guardrails, Postgres RLS multi-tenancy, real hardened auth.
- Real AWS ECS Fargate target — onboarding AND ongoing rollout
  (build → deploy → canary → promote → graduate/rollback), live-verified.
- Real Kubernetes target (Kind + Envoy Gateway) — still works, deprioritized.
- Autonomous git-push-triggered CI/CD loop — webhook + Gate 1 + Gate 2 +
  polling fallback, all live-verified end to end.
- Real CloudWatch telemetry for AWS-deployed projects (verification
  against genuine ALB/ECS metrics, not synthetic fallback data).
- ECS Fargate cost tracking, with real AWS Fargate pricing.
- Reports & Cost UI, with AI-generated digest summaries and AI-narrated
  gate-1 failure explanations (both via Groq, both with fallbacks).
- Onboarding wizard simplified to AWS ECS only (dead Kubernetes toggle
  removed).
- A real, working minimal test project (`smartcd-test-app`,
  pushed to `github.com/tusharharbinger-collab/CICD_Test`) that
  successfully builds, deploys, and serves a live status page through the
  real ALB — the reference example for "this is what a project that just
  works looks like."

---

## 6. What's left

Full, priority-ordered detail lives in `BACKLOG.md`. Headline summary,
most important first:

- **P0 — Blue-green deployment mode** (OPA rule, atomic-switch actuation,
  post-cutover automatic rollback, UI cutover view). Not started.
- **P1 — Wire AI right-sizing to the now-real CloudWatch data.** The
  computation (`compute_rightsizing_recommendation`) and the data source
  (`fetch_saturation_samples`) both exist and are both tested; nothing yet
  calls the first with the second's output.
- **P2 — Real bugs:** an orphaned ALB listener rule if a project's
  `path_prefix` changes between onboardings (low severity, not
  user-editable today).
- **P3 — Broader completeness:** Build/Test/Deploy as dedicated routes
  (currently wizard-step panels); Phase 7 (hosting the platform itself
  publicly, not just projects it deploys); ongoing visual refresh.
- **P4 — Deferred/operational:** TLS + real secrets store, OpenTelemetry
  tracing, `stage_logs` persistence, `tests/e2e/*.py` and
  `scripts/demo/*.sh` (both missing per the original assignment spec),
  MinIO (running, unused), Celery migration (spec names it; the
  sync+Redis-Streams implementation is functionally equivalent).

---

## 7. Key decisions and why (so they aren't re-litigated by accident)

**AWS ECS Fargate is the sole deploy target for new onboarding, decided
2026-09-16.** Kind is disqualified for "real hosting" (only resolves on
one laptop). Between the two real cloud-capable targets, ECS was chosen
over EKS: it's cheaper to leave always-on (no continuous control-plane
billing like EKS), and it had the more complete, more recently
live-verified path (onboarding *and* every ongoing rollout, not just first
deploy). **Known, accepted tradeoff:** EKS was the only target satisfying
the original assignment's graded Kubernetes/Gateway API rubric requirement
(`MASTER_BUILD_SPEC.md` §5.1/§5.3) — this choice trades that rubric line
item for a simpler, single-target backlog. The Kubernetes/Kind/EKS code
itself was **not deleted**, just deprioritized from the onboarding wizard;
`make kind-up` / `make eks-up` still work if this decision is revisited.

**The frontend's Docker image is a dev server, not a production build.**
Nobody changed this deliberately this session; it's called out here
because it was a repeated source of "why isn't my fix showing up" —
always `docker compose build frontend && docker compose up -d frontend`
after a frontend change, never just `restart`.

---

## 8. Real bugs found and fixed this session — full diagnostic trail

Every one of these was found by actually building, testing, or deploying
something for real — not by code review alone. Keeping the diagnostic
trail, not just the fix, since the *how it was noticed* is often the most
reusable part for the next debugging session.

### 8.1 — CloudWatch telemetry (verification-engine)

- **Wrong ARN-suffix extraction for target groups.** A load balancer ARN
  has an extra `/app/` path segment a target group ARN doesn't
  (`loadbalancer/app/name/id` vs. `targetgroup/name/id`). The shared
  "take the last 3 `/`-segments" logic that correctly stripped the prefix
  for load balancers left the whole `arn:aws:...:account-id:` prefix glued
  onto the target group dimension instead — meaning every CloudWatch query
  for a target group's metrics would have resolved to a dimension value
  that matched nothing. Caught by writing a real unit test with a real
  sample ARN, not by inspection. Fixed by splitting on `:` to isolate the
  resource part first, then handling the load-balancer/target-group cases
  differently (LB needs the `loadbalancer/` prefix stripped; TG needs
  nothing stripped — its resource part IS the dimension value).
- **Two separate instances of "`{}` is falsy in Python."** Both
  `main.py`'s `_fetch_metric_from_cloudwatch` (`if not cw_cfg:`) and
  `verification_task.py`'s `use_cloudwatch` decision
  (`any(m.get("cloudwatch") for m in metrics)`) treated a real, present
  `cloudwatch: {}` config block — which is exactly what every real
  project's generated YAML emits, since no per-metric query string is
  needed unlike Prometheus — as "absent" and skipped it. This meant the
  entire CloudWatch feature could never have fired for any real project,
  despite passing every test that used a non-empty dict. Fixed by checking
  `is None` / `"key" in dict` instead of truthiness.
- **Live verification caught it for real:** after fixing all three, a
  live `/verify` call against the real `payments-aws-baseline`/`-canary`
  target groups returned genuine, distinct CPUUtilization values
  (baseline ~45%, canary ~70%) that correctly triggered a real CUSUM
  detection — proof the whole chain (ARN resolution → CloudWatch query →
  statistical test) worked end to end, not just that it didn't crash.

### 8.2 — Stale pipeline status (api-gateway)

- `GET /api/v1/pipelines/runs/{id}`'s Postgres fallback path read raw
  `pipeline_executions.status`, which is written once as `'PENDING'` at
  trigger time and **never updated again** (the durable final status lives
  in `execution_state.status` — this exact trap is already documented in
  `CLAUDE.md`, and this endpoint just hadn't been updated to follow it).
  Any already-finished run whose 24-hour Redis cache key expired would
  report back as permanently "PENDING." Live-verified: cleared a real
  run's Redis key to force the fallback path, confirmed the fix correctly
  returns `FAILED` instead.

### 8.3 — Duplicated, unsynced Pause/Resume/Emergency Rollback controls (frontend)

- `PipelineDashboard.tsx` had the *correct* status-based `disabled` logic.
  But in the project-workspace layout, that component's own control bar is
  hidden (`hideRunControls`) — the controls actually rendered on screen
  belong to a **separate, duplicate copy** in `ProjectWorkspace.tsx`, which
  only checked `disabled={!selectedRunId}` — ignoring the run's actual
  status entirely. Confirmed live via a headless-browser check: on an
  already-`FAILED` run, all three buttons rendered fully clickable. Fixed
  by deriving the selected run's real status from the already-correct
  `listProjectRuns` API response (which uses the right
  `COALESCE(execution_state.status, pipeline_executions.status)` query)
  and gating all three buttons on it, matching `PipelineDashboard.tsx`'s
  own logic.

### 8.4 — Test commands were a silent, mandatory, wrong-by-default gate (pipeline-worker, api-gateway, frontend)

- The onboarding wizard defaulted `test_command` to a hardcoded,
  Python-specific `"pytest tests/"` **regardless of the project's actual
  language**. `worker.py` had a similar fallback baked in
  (`"pytest sample-app/v1.1.0/tests/ -v"`) for a pipeline stage that
  declares no command at all. For any non-Python project, this ran pytest
  against files it could never match, got pytest's own "no tests
  collected" exit code (5), and hard-failed the **entire pipeline** before
  it ever reached deploy — discovered live, onboarding a real JavaScript
  test project.
- Compounding problem: even a *correct* test command for the project's own
  language could fail purely on environment grounds, since
  `run_test_task` executes the command inside **pipeline-worker's own
  container**, which is `python:3.12-slim` — no Node.js, no Go, nothing
  but Python. A project's own Dockerfile very often already runs its real
  tests as a build layer (`RUN npm run test` before `RUN vite build`),
  which already gates the build for real; this separate stage was
  redundant at best and a false blocker at worst.
- **Fix:** test commands are now genuinely optional (blank → cleanly
  skipped, no guessed default) and non-blocking (a failure is logged and
  surfaced as a visible warning — `test_warning`, threaded through the
  Gate 1 callback and the onboarding preview UI — but never fails the
  pipeline). Applied identically in the real pipeline path (`worker.py`)
  and the Gate 1 dry-run path (`build_preview.py`). Verified with real
  integration tests run *inside* the actual container against real
  Postgres/Redis (not mocked) — confirmed a failing command doesn't block,
  and a missing one skips cleanly.

### 8.5 — `registry.internal` is not a real registry (the live 503 incident)

- The wizard auto-filled `container_image` as `registry.internal/{name}`
  for **every** project, regardless of deploy target — a hostname that
  only resolves inside the local docker-compose network. A real AWS ECS
  Fargate task has no way to reach it at all. The result: both the
  baseline and canary ECS services sat retrying a DNS lookup forever
  (`CannotPullContainerError: ... no such host`), never actually starting
  a task, and the ALB returned a straightforward 503 (no healthy targets)
  to any real visitor.
- **Diagnosis path that worked:** don't guess from the 503 alone — go
  straight to `ecs.describe_services(...)['events']`, which names the
  exact failure (`CannotPullContainerError`) and the exact bad image
  reference. This is the fastest way to root-cause an ECS deployment
  that "looks stuck."
- **Immediate fix (this one project):** created a real ECR repository by
  hand, corrected the project's stored pipeline YAML (`pipelines.
  policy_yaml` — **not** just the `projects.container_image` column, since
  the actual trigger flow reads the stored YAML) and the `projects` row
  directly via SQL, then triggered a fresh rollout.
- **Systemic fix (all future projects):** two changes. (1) The wizard no
  longer auto-fills a guaranteed-wrong value — `container_image` is now
  blank by default with clear guidance to use a real
  `<account-id>.dkr.ecr.<region>.amazonaws.com/<name>` URI, since no safe
  default exists without knowing the caller's own AWS account id. (2) ECR,
  unlike most registries, does **not** auto-create a repository on first
  push — `ensure_ecr_repository_exists` (idempotent, catches
  `RepositoryAlreadyExistsException`) now runs automatically in the build
  stage whenever the declared image is a real ECR URI, so a project's
  first-ever build can't fail on a missing repository again.

### 8.6 — The ALB doesn't strip path prefixes (the live 404 that followed the 503 fix)

- After fixing §8.5, the same URL started returning a genuine Flask 404
  instead of a 503. Diagnosis: hit the bare, unprefixed path
  (`/healthz`) — got the ALB's own "no service registered at this path,"
  confirming the ALB only forwards matching requests, and hit the
  *prefixed* path a second way (`/api/v1/cicd-test/healthz`) — got
  Flask's own 404 page, confirming the request DID reach the container,
  just at a literal path (`/api/v1/cicd-test/healthz`) nothing was
  registered for. This two-request test (bare path vs. prefixed path,
  reading which system's 404/error page comes back) is the fast way to
  tell "ALB never routed this" from "container got it and rejected it."
- **Root cause:** AWS ALB listener rules with a path-prefix condition
  **forward the full, unmodified path** to the target — there is no
  built-in path-rewrite/strip on a plain forward action. Any app using
  exact route matching (`@app.get("/healthz")`) will never match a
  request that actually arrives as `/api/v1/cicd-test/healthz`.
- **This is not fixable in the platform** — it's an AWS ALB limitation,
  and the shared-ALB-with-path-prefixes design (one ALB for every project,
  to avoid per-project cost) makes prefix-based routing structurally
  necessary. **Every onboarded app must tolerate an unknown path prefix.**
  The working pattern (used in the reference `smartcd-test-app`): a
  catch-all route matched on the *last* path segment rather than the full
  path —
  ```python
  @app.route("/", defaults={"path": ""})
  @app.route("/<path:path>")
  def catch_all(path):
      tail = path.rstrip("/").rsplit("/", 1)[-1]
      if tail == "healthz":
          return jsonify({"status": "ok"}), 200
      if tail == "hello":
          return jsonify({"message": "..."})
      return render_index()
  ```
  This is also why the *other* real project onboarded this session
  (`test-`, an nginx-served static site) never hit this: nginx's own
  `try_files $uri $uri/ /index.html;` SPA-fallback pattern is naturally
  prefix-tolerant by accident.
- Also worth remembering: **ECS target group health checks bypass the ALB
  path-prefix rule entirely** — they hit the container directly via its
  task's private IP on the configured `health_check_path`, unprefixed.
  That's why `describe_target_health` can report "healthy" even while
  real ALB traffic 404s — they're two independent code paths, and a
  healthy target group does not by itself prove the live URL works.
  Always test the actual public URL, not just target health.

---

## 9. Deployment runbook — onboarding a new project so it works the first time

Distilled from every failure in §8. Follow this and a new project should
need zero follow-up fixes:

1. **Repo:** public repo → use the "Public Git Repository" tab, no GitHub
   connection needed. Private repo → connect GitHub first.
2. **Dockerfile:** have a real one if at all possible — it's the most
   reliable path, and if it already runs your tests as a build layer
   (`RUN npm test` / `RUN pytest` before the final build stage), that
   *is* your real test gate; leave the wizard's test command blank.
3. **Container port:** must match what your Dockerfile actually
   `EXPOSE`s / your app actually binds to. The wizard defaults to 8080 —
   change it if your app uses something else (e.g. nginx's default 80).
4. **Health check path:** the default `/healthz` works even for apps that
   don't define that exact route, *if* the app has a catch-all/SPA-style
   fallback (see #6). If it 404s on unknown routes, either add the real
   route or point this field at one that exists.
5. **Container image registry:** must be a **real** ECR URI in your own
   AWS account:
   `<account-id>.dkr.ecr.<region>.amazonaws.com/<name>` (lowercase). The
   repository does not need to exist beforehand — the build stage creates
   it automatically now (§8.5) — but the account id and region must be
   real; there is no safe default the platform can guess.
6. **Route design — the one genuinely easy-to-miss requirement:** your app
   will receive requests at `<your assigned path prefix>/...`, never at
   bare `/...`, because the ALB cannot strip that prefix (§8.6, permanent
   AWS limitation). Use a catch-all matched on the last path segment (see
   the code snippet in §8.6), or an SPA-style fallback (nginx `try_files`
   or equivalent) — not exact route matching alone.
7. **Test command:** optional. Leave blank if your Dockerfile already
   tests itself. If you do set one, it runs in `pipeline-worker`'s own
   container (Python only — no Node.js, no Go), and its failure is now
   non-blocking (a visible warning, not a hard stop) — so it's safe to set
   even if you're not 100% sure it'll match, but for it to be a
   *meaningful* gate, it needs to be a real Python command.
8. **Deploy target:** AWS ECS is the only option offered; nothing to pick.
9. **After first deploy:** hit the live URL a few dozen+ times before
   checking the Verification Inspector — the statistical tests need
   N≥100 real samples before they'll produce a confident `HEALTHY`
   verdict (a hard invariant, not a bug); until then you'll correctly see
   `DEGRADED`/"insufficient samples."

---

## 10. Troubleshooting quick-reference

| Symptom | Likely cause | How to confirm | Where it's covered |
|---|---|---|---|
| Live URL returns 503 | ECS service can't get a healthy task running | `ecs.describe_services(...)['events']` — look for `CannotPullContainerError` or a failing container health check | §8.5 |
| Live URL returns 404, but target group shows "healthy" | ALB forwarded the full path prefix; app has no route for it | Hit the bare unprefixed path (should get the ALB's own "no service registered" message) vs. the prefixed path (should get your app's own 404) | §8.6 |
| Pipeline fails at the "test" stage | Should no longer happen — test failures are non-blocking as of this session (§8.4). If it's still failing the whole pipeline, that's a regression to investigate, not expected behavior. | Check `worker.py`'s test-stage block still has the try/except around `run_test_task` | §8.4 |
| A run's status shows "PENDING" forever in some view but "FAILED"/"COMPLETED" in another | A query is reading raw `pipeline_executions.status` instead of `COALESCE(execution_state.status, pipeline_executions.status)` | Check the SQL directly | §8.2, also documented in `CLAUDE.md` |
| CloudWatch-backed verification always shows 0 requests / no data | ARN dimension resolution or the `cloudwatch: {}` truthiness trap — both should be fixed now (§8.1), but if it recurs, check `resolve_target_group_dimension`'s output against a real `describe_target_groups` call by hand | §8.1 |
| First build of a new AWS ECS project fails on registry push | Either the image name isn't a real ECR URI (check for `registry.internal` — should be impossible now, wizard field is blank by default), or (older code) the ECR repo doesn't exist — should now auto-create | §8.5 |
| Frontend code change isn't showing up after editing | The frontend Docker image bakes in source at build time — `docker compose build frontend && docker compose up -d frontend`, not just a restart | §3, §7 |
| Emergency Rollback / Pause / Resume behaves wrong for a run's actual state | Check whether you're looking at `PipelineDashboard.tsx`'s controls (hidden in the project workspace) or `ProjectWorkspace.tsx`'s own copy — verify both are reading real status, not `!selectedRunId` | §8.3 |

---

## 11. Useful live-debugging snippets

Run these via `docker compose exec pipeline-worker python -c "..."` (or
any service with `boto3` installed) against the real AWS account.

**Check real ECS service state (the fastest way to root-cause a stuck deploy):**
```python
import boto3
ecs = boto3.client('ecs', region_name='us-east-1')
d = ecs.describe_services(cluster='smartcd-platform', services=['<project>-baseline'])['services'][0]
print(d['runningCount'], d['desiredCount'])
for e in d['events'][:5]:
    print(e['message'])
```

**Check real ALB target health (independent of, not a substitute for, hitting the real URL):**
```python
import boto3
elbv2 = boto3.client('elbv2', region_name='us-east-1')
arn = elbv2.describe_target_groups(Names=['<project>-baseline'])['TargetGroups'][0]['TargetGroupArn']
for h in elbv2.describe_target_health(TargetGroupArn=arn)['TargetHealthDescriptions']:
    print(h['TargetHealth']['State'], h['TargetHealth'].get('Reason'))
```

**Find a project's real stored pipeline YAML (what actually executes on trigger, not just the `projects` table):**
```sql
SELECT policy_yaml FROM pipelines WHERE pipeline_id = (
  SELECT pipeline_id FROM projects WHERE name = '<project name>'
);
```

**Find a run's real status regardless of which table's showing stale data:**
```sql
SELECT e.pipeline_run_id, e.trigger_type, COALESCE(es.status, e.status) AS real_status
FROM pipeline_executions e
LEFT JOIN execution_state es ON es.pipeline_run_id = e.pipeline_run_id
WHERE e.pipeline_id = '<pipeline_id>'
ORDER BY e.started_at DESC LIMIT 10;
```

---

## 12. Housekeeping notes

- Most of this session's work is **uncommitted** in the working tree as of
  this writing — run `git status` before assuming anything described here
  is on `main`.
- The demo account: `demo@acme-corp.test` / `acme-demo-2026` (tenant
  `acme-corp`). A second, intentionally-empty tenant exists to verify
  isolation: `demo@other-corp.test` / `other-demo-2026`.
- Real AWS account used throughout: **236087863083**, region
  **us-east-1**. Never paste AWS credentials into chat — use `.env` or
  `aws configure` locally instead (this was flagged once this session as
  a real security concern).
