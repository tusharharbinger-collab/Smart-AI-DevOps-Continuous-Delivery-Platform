# Smart AI DevOps & Continuous Delivery Platform — Deep Dive

This is the "understand everything" document. It assumes no prior context and builds up from
first principles: what the project is, why it's built the way it is, what every moving part
does, and how a single `git push` turns into safe, verified, live traffic on AWS. Read it
top to bottom once, then use it as a reference.

---

## 1. What this actually is, in one paragraph

Most CI/CD tools stop at "run the pipeline and hope." This platform adds a layer most tools
don't have: after a new version deploys next to the old one, the platform **statistically
compares their real, live behavior** (error rate, latency, resource saturation, a business
metric) and **decides on its own** — with evidence, not a guess — whether to promote the new
version to 100% traffic or roll it back. Every one of those decisions is signed, policy-gated,
and explained in plain language. It's a real multi-service platform (not a script), with a
real web UI, deployed against a real cloud target (AWS ECS Fargate, with a Kubernetes path
that also works), multi-tenant from day one.

**The mental model in one sentence:** *build → deploy the new version next to the old one →
compare their real telemetry with real statistics → let policy (not a human, not a hard-coded
threshold) decide what happens next → explain the decision → record it forever.*

---

## 2. The problem this solves, and why it's hard

A deploy succeeding (container starts, health check passes) tells you almost nothing about
whether the new code is actually *good*. The old way: ship it, then a human stares at a
dashboard for 20 minutes hoping nothing looks wrong. That doesn't scale, and it's not really a
decision — it's a vibe check.

The right way, and the hard part, is:
1. Run the new version next to the old one (canary) or fully replace it and be ready to snap
   back (blue-green) — never just "replace and pray."
2. Collect **real** metrics from both versions, split by cohort, so the comparison is
   apples-to-apples.
3. Run a **real statistical test** — not `if error_rate > 1%` (a threshold dressed up as
   intelligence) — because a threshold can't tell "the traffic pattern changed" apart from
   "the new version is broken," and it can't express confidence.
4. Gate every automatic action behind a policy the reasoning layer **cannot bypass by
   construction** — because an autonomous system that can occasionally promote something it
   shouldn't, or roll back something healthy, is worse than no automation at all.
5. Explain *why* — an unexplained autonomous rollback is treated as a failure in its own right,
   independent of whether the rollback itself was the right call.

Everything below exists in service of those five points.

---

## 3. High-level architecture

```
                        ┌─────────────────────────────────────────────┐
                        │                 frontend (React)             │
                        │  /projects — overview, wizard, workspace     │
                        └───────────────────────┬───────────────────────┘
                                                 │ REST + WebSocket + SSE
                        ┌───────────────────────▼───────────────────────┐
                        │                 api-gateway (FastAPI)          │
                        │  auth · RLS session · projects · webhooks      │
                        │  · reports · audit · policy · logs streaming   │
                        └──────┬───────────────────────────┬────────────┘
                                │ Redis Streams               │ Postgres (RLS)
                 ┌──────────────▼─────────────┐  ┌────────────▼─────────────┐
                 │      pipeline-worker         │  │  verification-engine     │
                 │  parses pipeline YAML → DAG   │  │  statistical/ML compare  │
                 │  build → test → deploy →      │  │  baseline vs canary      │
                 │  canary_loop (stats OR         │→ │  → signed ImmutableVerdict│
                 │  blue-green health-gate)       │  │  → stream:verdicts       │
                 └──────┬──────────────────┬─────┘  └────────────┬─────────────┘
                        │                  │                       │
             real deploy calls      real deploy calls      verified verdict
              (Kubernetes)            (AWS ECS)                    │
                        │                  │           ┌───────────▼─────────────┐
                        ▼                  ▼           │     policy-controller     │
              Kind + Envoy Gateway   ECS Fargate +      │  verify signature → OPA   │
              (HTTPRoute weights)     shared ALB        │  → actuate traffic weight │
                                     (listener weights)  │  or alert or audit-only   │
                                                          └───────────┬───────────────┘
                                                                       │
                                                          ┌────────────▼────────────┐
                                                          │  explainability-service   │
                                                          │  Groq-backed RCA/digest   │
                                                          │  grounded in real evidence│
                                                          └──────────────────────────┘
```

Every one of those boxes is a **separate Docker container**, independently deployable, with
its own `Dockerfile` and `requirements.txt`. They only share code through `shared/`, which is
mounted read-only into every container — there's no shared Python package otherwise, because
each service is a genuinely separate build context.

---

## 4. The five backend services, one at a time

### 4.1 `api-gateway` (FastAPI, Python)

The only service the outside world (browser, GitHub webhooks) ever talks to directly. Owns:

- **Auth**: bcrypt password hashing, HS256-signed 15-minute access tokens, a 7-day opaque
  refresh token held in Redis as a revocable allow-list (rotated every use, revoked on
  logout), Redis-backed login rate-limiting (5 failures → 429), an `auth_events` audit trail,
  and role checks (`require_role()`) actually enforced on sensitive actions — not just carried
  in the JWT and trusted blindly.
- **Multi-tenancy**: every request's Postgres session runs `SELECT set_config('app.active_tenant_id', ...)`
  before any query, and every tenant-scoped table has an RLS policy keyed on that setting. The
  app's own database user (`app_user`) is a non-superuser specifically so RLS can't be
  silently bypassed — the separate `platform` superuser DSN is deliberately never used by the
  running app.
- **Projects**: the onboarding wizard's backend — repo detection, pipeline YAML generation,
  triggering rollouts, reading back status/reports/cost/audit.
- **GitHub integration**: a real OAuth 2.0 Authorization Code flow (token lives in Redis, never
  Postgres — this service has no `cryptography` dependency on purpose) plus the webhook
  receiver that makes the whole thing autonomous (§6).
- **Live log streaming**: WebSocket for structured pipeline events, a separate `fetch()`-based
  SSE stream for raw log lines (a real trap: the browser's native `EventSource` can't send an
  `Authorization` header, so it can't carry the JWT this platform requires — every SSE consumer
  streams over `fetch()` instead and parses frames itself).

### 4.2 `pipeline-worker` (FastAPI, Python)

The executor. Parses a project's declarative pipeline YAML into a DAG (`pipeline/dag_builder.py`,
using `networkx`), then runs each stage — `build → test → deploy → canary_loop` — for real:

- **build**: clones the repo, finds/synthesizes a Dockerfile (§9), runs `docker build`, pushes
  to ECR.
- **test**: runs the project's own test command in an isolated venv; deliberately **never
  blocks the pipeline** — a failure here is logged as a warning and surfaced to the human, but
  the real gate is the app's own Dockerfile/build succeeding, not a guessed default test
  command.
- **deploy**: pushes the new image to the *canary* cohort only — never touches traffic weight
  (that's a separate, policy-gated actuation, always).
- **canary_loop**: either (a) the statistical path — register the ramp's real steps in Redis,
  wait for verification-engine's verdict — or (b) the blue-green health-gated path (§8), which
  never calls verification-engine at all.

It listens on two Redis Streams (`stream:pipeline:start`, `stream:gate1:check`) via consumer
groups — not bare pub/sub — so running 2+ replicas never double-processes the same run.
`pipeline/reconciler.py` resumes any pipeline left `RUNNING` if the worker process died,
picking up from the exact stage it was on, using Postgres (not just Redis) as the durable
source of truth for execution state.

### 4.3 `verification-engine` (FastAPI, Python 3.11, deliberately isolated)

The statistics. Takes a pipeline's declared metrics, dispatches each one by its `category` to a
real statistical test module (table in §7), combines them into a verdict, HMAC-signs it, and
publishes it to `stream:verdicts`. **This is the one service structurally barred from touching
Kubernetes or AWS actuation** — no `kubernetes` dependency, no AWS mutation calls, no kubeconfig
mount. It reads telemetry (Prometheus or CloudWatch) but never acts on what it concludes. That
separation is load-bearing: the reasoning layer (verification-engine) and the actuation layer
(policy-controller) are different services on purpose, so a verdict can never *become* an
action except by going through a second, independent, policy-gated hop.

### 4.4 `policy-controller` (FastAPI, Python)

The gatekeeper. Reads verdicts off `stream:verdicts` (its own consumer group), **verifies the
HMAC signature and a freshness window before doing anything else** — an unverified verdict is
never acted on — then evaluates it against the single OPA policy (`policies/delivery_guardrails.rego`,
§10). Only on `allow_action = true` does it call `actuation_executor.py` (Kubernetes) or
`aws_actuation_executor.py` (ECS) to actually shift traffic weight, or `alert_dispatcher.py`/
`audit_writer.py` for a block/alert. `rollout_scheduler.py` owns the progressive-ramp lifecycle:
advancing to the next step, scheduling the next reverify once the step's real `minDuration`
elapses, pausing for manual approval when a step requires it, graduating the final step, and
manual/emergency rollback.

### 4.5 `explainability-service` (FastAPI, Python)

The narrator, never the decider. Builds exact-citation explanations from a verdict's real
evidence (which metric, by how much, over how many samples) and calls Groq
(`llama-3.3-70b-versatile`, OpenAI-compatible `/chat/completions` via `httpx`, no SDK
dependency) for RCA reports and periodic digests — always within a hard time budget, always
with a deterministic, non-AI fallback if the call fails or isn't configured. Nothing here ever
influences a promote/rollback decision; it only explains a decision already made.

---

## 5. The frontend (React + TypeScript + Vite)

One authenticated console at `/projects`:
- **`ProjectsOverview.tsx`** — the grid of every project, live status.
- **`NewProject.tsx`** — the 3-step onboarding wizard: pick a repo (GitHub OAuth or a public
  URL), auto-detect (or declare) how it builds, confirm networking/resources, see the
  ML-based Repo Health & Cost Prediction (§12).
- **`ProjectWorkspace.tsx`** — hosts four tab screens as nested routes, all reading
  `{pipelineId, pipelineRunId, tenantId, projectId}` from router outlet context so the exact
  same components work regardless of which project you're looking at:
  - **Pipeline View** (`PipelineDashboard.tsx`) — the real-time stage timeline, live logs,
    trigger/pause/resume/emergency-rollback.
  - **Verification Inspector** — baseline-vs-canary metrics, the verdict, confidence, and
    the grounded explanation.
  - **Policy & Gates** (`PolicyManager.tsx`) — the declarative policy this project runs under.
  - **Audit Ledger** — every signed actuation, ever, exportable as a SOC-2-style CSV.
  - **Reports** / **Cost** — the digest, per-deployment report, and cost tracking tabs.

Real-time state comes from two different mechanisms for a real reason: `usePipelineEvents`
(WebSocket) carries structured stage/status events; `useLiveLogs` (a hand-rolled `fetch()`-based
SSE reader) carries raw log lines, because the browser's native `EventSource` API has no way to
attach the `Authorization` header this platform's auth requires.

---

## 6. Full walkthrough: what happens after `git push`

This is the "thinking flow" — the actual sequence of real systems talking to each other,
traced end to end.

1. **GitHub sends a webhook** (or, if that delivery is ever missed, api-gateway's own
   `poll_for_missed_webhook_deliveries` periodic loop catches it later by comparing each
   connected repo's real branch HEAD against the last commit actually checked). Either path
   HMAC-verifies the signature and dedupes on GitHub's own `X-GitHub-Delivery` ID.
2. Neither path deploys directly — both call one shared `_queue_gate1_check` helper, which
   XADDs onto `stream:gate1:check`.
3. **Gate 1** (pipeline-worker's consumer): runs a real, isolated build+test dry run
   (`build_preview.py` — the exact same function the onboarding wizard's "does this build"
   check uses) against the new commit. Calls back `POST /internal/{project_id}/gate1-result`.
   `passed=False` **never touches `pipeline_executions` at all** — it gets an AI-narrated
   failure explanation instead and stops there. Only `passed=True` proceeds.
4. That callback calls `_trigger_rollout_internal` — the exact same function a human clicking
   "Trigger New Rollout" in the UI calls. There is no second, parallel "autonomous trigger"
   code path to keep in sync; a webhook-triggered run and a human-triggered run are
   indistinguishable downstream.
5. A real `pipeline_executions` row is created, and the run is XADDed onto
   `stream:pipeline:start`.
6. **pipeline-worker** picks it up, builds the DAG, and runs `build → test → canary_deploy`.
7. At `canary_verify`, one of two things happens depending on the project's `deploy_mode`:
   - **Canary (statistical)**: the real ramp steps (e.g. 10% → 25% → 50% → 100%, each with a
     real `minSampleSize`/`minDuration`) are registered in Redis. verification-engine pulls
     real telemetry for both cohorts, runs the matching statistical test(s) per metric
     `category`, produces a signed `ImmutableVerdict`, and XADDs it to `stream:verdicts`.
   - **Blue-green (health-gated)**: no statistics at all — see §8.
8. **policy-controller** (canary path) verifies the verdict's signature, evaluates it against
   OPA, and — only if allowed — shifts real ALB/HTTPRoute traffic weight and schedules the next
   step's reverify once its `minDuration` has actually elapsed. A step flagged
   `requiresManualApproval` (by default, the final 100% cutover) pauses and fires an alert
   instead of proceeding.
9. Once the final step promotes, **graduation** makes it durable: reads the canary's *live*
   image off the real cluster/ECS service (never the originally-requested tag, in case it
   drifted), patches the baseline to match, resets weights to 100/0, idles the canary — and
   only updates the DB's `active_production_tag` if the real infrastructure mutation actually
   succeeded (never claim a version is live that isn't).
10. Every actuation along the way — weight change, rollback, graduate — is written to
    `audit_ledger`, HMAC-verified, queryable forever.
11. **Alerts** fire at the moments that matter: a rollback fires, a promotion is blocked
    pending approval, or verification can't reach a confident verdict in the allotted time —
    pushed out, never requiring someone to be watching a dashboard live.

---

## 7. The verification engine — every statistical test, and why that one

| Metric category | Test | Why this test specifically |
|---|---|---|
| `error_rate` | Wald SPRT (sequential probability ratio test, Bernoulli likelihood) | Purpose-built for "is this proportion different from a reference, and how confident am I, as evidence accumulates" — exactly the shape of an error-rate comparison, and it can conclude *early* with fewer samples when the signal is strong, unlike a fixed-sample test. |
| `latency` | Mann-Whitney U (+ Kolmogorov-Smirnov as a secondary check) | Latency distributions are never normal (long right tail) — Mann-Whitney is the standard non-parametric test for "is one distribution shifted relative to another" without assuming a shape. KS adds a second, independent check on the whole distribution, not just its central tendency. |
| `saturation` (CPU/memory/etc.) | CUSUM + BOCPD (both hand-implemented in NumPy, not the `ruptures` library) | These are change-point detection methods — saturation problems often show up as a *drift* or a *step change* over time, not a single bad sample, which a simple mean-comparison test would miss entirely. |
| `business_metric` (e.g. conversion rate) | Fisher's exact test / Chi-square (auto-selected by expected cell counts) | A business metric is usually a count/proportion comparison across two small-ish samples — Fisher's exact is exact and safe at small sample sizes where Chi-square's approximation breaks down; the code switches to Chi-square automatically once counts are large enough that Fisher's exact combinatorics would be needlessly expensive. |
| cross-metric | Isolation Forest (scikit-learn) | Trained on the baseline's multivariate saturation vectors, scores the canary's vectors for how anomalous they are as a *joint* pattern across multiple signals at once — catches a correlated multi-metric problem that no single-metric test would flag on its own. |

`scoring/confidence.py` folds sample sufficiency, variance stability, and elapsed-time
stability into one `C ∈ [0,1]` confidence value. Any critical-tier metric breach, or any
business-metric regression, is an immediate hard `FAILED` regardless of the composite score —
a genuinely bad critical signal is never allowed to be "averaged away" by unrelated good
metrics.

**Hard rule enforced in two independent places** (Python `confidence.py` *and* OPA's
`minSampleSize`, so one can't be silently weakened without the other): no verdict fires below
N ≥ 100 samples.

---

## 8. Blue-green mode — the problem it solves, and how it works

A brand-new project with **zero real visitors** can never accumulate the samples the
statistical tests above need — it would sit `DEGRADED` ("insufficient samples") forever and
never go live, even though the build and deploy themselves succeeded. Blue-green is a second,
separate deployment strategy for exactly this case: it **never runs statistical verification
at all**, and instead gates the cutover on real infrastructure health:

1. Deploy the new version to the "green" ECS service.
2. Wait for the ECS service itself to stabilize (`runningCount == desiredCount`).
3. Wait for the ALB target group to report the *new* task specifically healthy (see the real
   bug story in §14 — this used to be far less precise than it sounds).
4. `HEALTH_GATED_CUTOVER` (an OPA rule that checks only the freeze window, not a verdict) —
   shift 100% of traffic atomically.
5. **Real live-URL verification**: an actual HTTP GET through the real ALB, with the real path
   prefix — not just a target-group health check, which bypasses the ALB's own routing
   entirely and would miss an ALB-specific bug.
6. If that fails → `rollback_blue_green_ecs_weights` reverts traffic and the pipeline reports
   `FAILED`, never `COMPLETED`.
7. If it succeeds → graduate: promote the new image onto baseline, reset weights, idle green.

---

## 9. Universal Dockerfile synthesis — "any repo" without a human writing a Dockerfile

If a repo has no `Dockerfile`, `shared/repo_scanner.py` does **pure, deterministic file-
signature detection** (never an LLM guessing) — a real manifest file (`requirements.txt`,
`package.json`, `go.mod`, ...) implies a language, and for Node specifically, real dependency
names (`next`, `vite`, `react-scripts`) in `package.json` imply a framework
(`node-server` / `spa` / `nextjs`). `dockerfile_synthesis.py` then writes a minimal, standard
Dockerfile for whichever of `python` / `node` (three sub-templates) / `go` / `static` was
detected, and it gets built through the **exact same `docker build` call** a human-authored
Dockerfile would — no separate, less-tested code path.

The one genuinely tricky part: the platform's shared ALB routes every project by a path prefix
it **cannot strip** (a real, permanent AWS limitation — a plain forward action has no
path-rewrite capability). A synthesized nginx-served app (SPA/static) would otherwise 404 on
every asset request, because nginx has no idea it's being reached at `/api/v1/your-app/...`
rather than `/`. The fix (found and closed this session): the project's real path prefix is
passed into the container as a runtime env var (`PATH_PREFIX`), and a small `envsubst`-based
entrypoint script templates nginx's config at **container start** — `location <prefix>/ { alias
/usr/share/nginx/html/; ... }` — stripping the prefix internally. One image works for every
project regardless of its assigned prefix; nothing is baked in at build time.

---

## 10. Guardrails — OPA and the policy that gates every action

`policies/delivery_guardrails.rego` is the **single** policy every actuation must clear —
there is deliberately one file, not one per concern, so there's one place to audit. It covers:
freeze windows (blocked deploy times), minimum confidence/sample size before a decision is
allowed to fire, manual-approval-required stages, a cost-delta ceiling, right-sizing
auto-apply rules, first-deployment handling, and the blue-green health-gated cutover rule.
`policies/tests/guardrails_test.rego` (run via `opa test policies/`) covers it with real
adversarial cases — this is the thing `tests/adversarial/test_guardrail_bypass.py` actively
tries to defeat with deliberately misleading telemetry, because a bypass here is treated as a
critical failure, full stop.

**The boundary that can't be crossed by construction**: policy-controller only ever acts on a
verdict it (a) received off the real `stream:verdicts` stream and (b) independently
HMAC-verified. There's no code path where "what the reasoning layer concluded" can become "what
the actuator does" without going through OPA in between. Even blue-green, which skips
statistics entirely, still goes through its own OPA rule (`HEALTH_GATED_CUTOVER`) before
cutting over — nothing actuates unconditionally.

---

## 11. AWS — every service actually used, and exactly why

The platform runs one shared ECS cluster (`smartcd-platform`) and one shared Application Load
Balancer (`smartcd-platform-alb`) for **every** onboarded project — not one cluster/ALB per
project. This keeps cost and operational surface down; isolation between projects comes from
each getting its own ECS services, target groups, and ALB listener path-pattern rule, not from
separate infrastructure.

| AWS service | boto3 client | What it's used for here |
|---|---|---|
| **ECS (Fargate)** | `ecs` | Registering task definitions (image, cpu/memory, env vars, log config), creating/updating services, scaling, describing running tasks — the actual compute. Fargate specifically = no EC2 instances to manage. |
| **ELBv2 (the ALB)** | `elbv2` | Creating a target group per project per cohort (`{service}-baseline` / `{service}-canary`), a listener rule matching the project's path prefix, and — this is the traffic-shifting mechanism — adjusting the **weights** on the forward action's target groups. Never replica-count games; always real weighted routing. |
| **EC2** | `ec2` | Only used read-only, at onboarding, to look up the default VPC's public subnets and to find/create the task security group — infrastructure discovery, not compute. |
| **ECR** | `ecr` | `ensure_ecr_repository_exists` (idempotent) creates a project's image repository automatically on first push — unlike most registries, ECR doesn't auto-create one, so this closes a real onboarding gap where a first deploy would otherwise fail. Also used for `get_authorization_token` (Docker login credentials for the build stage's `docker push`). |
| **CloudWatch** | `cloudwatch` | Real telemetry source for AWS-deployed projects — the same statistical tests read genuine `GetMetricData` results (ALB/ECS-level CPU, latency, request count) instead of the docker-compose-only Prometheus path. |
| **IAM** | `iam` | Only at onboarding, to reference/attach the task execution role ECS needs to pull images and write logs — no IAM users/policies are created by the app itself. |
| **CloudWatch Logs** | `logs` | Task log group creation/configuration so a container's stdout/stderr is actually captured somewhere queryable. |

**What's deliberately *not* used:** Terraform or any other IaC tool. Every AWS object this
platform needs is created **idempotently in Python** at the moment it's needed (`ensure_*`
functions that check-then-create) — a real, considered tradeoff for a project whose
infrastructure shape is "one shared cluster/ALB, N per-project services," not something that
benefits much from a separate declarative-infra layer. No AWS API Gateway, no Lambda, no
CloudFront — the whole real-time/actuation path is deliberately just ECS + ALB + CloudWatch.

---

## 12. The newest piece: ML-based repo report + cost prediction

Before a repo is even onboarded, `GET /repos/{owner}/{repo}/repo-report` (api-gateway,
`github_router.py`) answers two questions no other part of the platform answers pre-deploy:

- **"Is this repo in reasonable shape to host?"** — `shared/repo_report.py` extracts real,
  deterministic structural features from the same file-tree fetch build-detection already does
  (has tests? a lockfile? CI config? a README/LICENSE? how many dependencies?), then fits a
  real `sklearn.ensemble.IsolationForest` (the exact same library/pattern already used in
  verification-engine's cross-metric anomaly test) against a small bundled reference corpus of
  known-good repo profiles. The output is a risk score **plus which specific signals are
  unusual** — never a bare unexplained number.
- **"What will this cost to run?"** — pure arithmetic (deliberately *not* ML, since a real
  formula beats a guess): the platform's actual default ECS task sizing (256 CPU units / 512
  MiB) times the real, published Fargate hourly rates, giving a steady-state monthly estimate
  and a rollout-window estimate — using the exact same pricing constants and cost-delta formula
  the platform's real post-deploy `cost_analysis` tracking already uses, so the pre-deploy
  estimate and the post-deploy real bill are apples-to-apples.

An optional Groq-narrated one-line summary sits on top, with a template-based fallback if Groq
is unreachable — same split as everywhere else: a real detector produces the facts, an LLM only
ever narrates them.

---

## 13. Docker & the full container inventory

`docker compose up --build` brings up every one of these:

| Container | Role |
|---|---|
| `postgres` | The durable store — projects, pipelines, execution state, verdicts (via `verification_records`), audit ledger, cost analysis, auth. RLS-enforced multi-tenancy. |
| `redis` | Fast-path execution state, Streams (the trigger/verdict/gate1-check queues), the refresh-token allow-list, rate-limiting, the GitHub webhook repo→tenant registry. |
| `opa` | The single policy decision point — `policies/delivery_guardrails.rego` loaded at container start (⚠️ **does not hot-reload** — a `.rego` change needs `docker compose restart opa`, even though the directory is a live volume mount). |
| `minio` | S3-compatible object storage, stood up but not yet written to by the app. |
| `prometheus` | Scrapes the sample-app baseline/canary containers for the docker-compose-local telemetry path. |
| `loki` + `promtail` + `grafana` | Platform observability — structured logs shipped and queryable, dashboards. |
| `sample-app-baseline` / `sample-app-canary` | A real toy app with two cohorts, used to demonstrate/verify the whole pipeline locally without needing AWS. |
| `load-generator` | Synthetic traffic so the sample app's cohorts have something to compare. |
| `api-gateway`, `pipeline-worker`, `verification-engine`, `policy-controller`, `explainability-service` | The five backend services, §4. |
| `frontend` | The React app (also runnable as a local Vite dev server for faster iteration — same code either way). |

Every backend service has its **own** `Dockerfile` and `requirements.txt` — genuinely separate
dependency sets, which is exactly why a bug like "I added a function to `shared/` that imports
`requests`, but one service that imports it doesn't have `requests` in its own
`requirements.txt`" is a real class of bug this project has hit and had to fix by checking each
container's actual dependencies, not just running tests locally in one shared venv (a local
test venv has every dependency installed regardless of which service's container would really
have it — it can't catch this).

---

## 14. Real bugs found and fixed by actually running the system

Not hypothetical — each of these was found by triggering a **real** pipeline run against the
**real** AWS account, not by reading code:

- **The trailing-slash bug**: the platform's own computed `live_url` (and the deployed app's
  own routing) didn't guarantee a trailing slash, so a browser resolved the page's relative
  `<link href="style.css">` against the wrong base path — the app rendered completely unstyled.
  Fixed in two places: the app's own server code, and the platform's `build_live_url()` so it
  never happens again for any future project.
- **The ALB path-prefix vs. synthesized nginx bug** (§9): a Vite/CRA app built with default
  absolute asset paths, deployed under a non-root ALB prefix nginx didn't know about, silently
  served the SPA fallback (`index.html`) for every asset request instead of the real file — a
  CSS/JS request got HTML back. Verified fixed with a real `docker build` + real HTTP checks:
  `curl .../assets/app.js` now returns real JS with the right content-type, not HTML.
- **The blue-green rollback race condition** — the most serious one. `wait_for_target_group_healthy`
  used to require *every* currently-registered target to be healthy, including a stale target
  still draining from the *previous* deployment. That's wrong two ways: it could time out
  forever on a target that would never recover, and — far worse — it could report "healthy"
  using *only* the old target's state, before the new task had even registered at all. Proven
  live: a deliberately-broken app got graduated to 100% real production traffic, undetected,
  because the post-cutover live-URL check got lucky and hit the still-good old task instead of
  the new broken one. Fixed by (1) identifying the specific task belonging to the current
  deployment by private IP and requiring only it to be healthy, and (2) also requiring every
  *other* target to be fully gone (not just draining) before declaring the gate passed, backed
  by shortening the target group's deregistration delay from AWS's 300s default to 30s.
  Re-drilled live after the fix: the same deliberately-broken app now correctly triggers a real
  `ROLLBACK` audit entry — the first one this platform had ever recorded.
- **Empty Audit Ledger / Cost tabs for a completed blue-green run**: blue-green's cutover path
  never called `record_actuation`/`record_cost_analysis` at all (those calls only ever existed
  on the statistical-verdict path), and separately, the database's own `audit_ledger.action`
  CHECK constraint didn't even include `'BLUE_GREEN_CUTOVER'` as a legal value — a genuinely
  latent schema bug nobody had hit yet. Fixed both.
- **Duplicate pipeline runs for one commit**: a stale-message reclaim threshold (30s) shorter
  than how long a real build+test stage actually takes, so a still-in-progress run's message
  got reclaimed and reprocessed by a second worker. Fixed by raising the threshold well past
  any real stage's legitimate duration, with a regression test pinning the relationship.

---

## 15. API reference (real endpoints, grouped by service)

**api-gateway** (external surface, `/api/v1/...`):
`auth` (login/refresh/logout) · `pipelines` (CRUD, runs, pause/resume/rollback, logs stream) ·
`verification` · `policies` · `reports` (digest, per-deployment) · `audit` (list + SOC-2 CSV
export) · `services` (legacy K8s onboarding) · `projects` (create/list/delete, rollout trigger,
webhook registration, AI pipeline generation, runs/stages/rollout/logs/rollback/approve,
cost-history, audit) · `integrations/github` (status/authorize/callback/disconnect/repos/
branches/build-detection/**repo-report**) · `integrations/registry` (stored credentials) ·
`webhooks/github` · a WebSocket at `/ws/pipelines`.

**pipeline-worker** (internal): `services/onboard[-aws]`, `services/deprovision[-aws]`,
`services/onboard-aws/traffic-weights`, `pipelines/start`, `pipelines/validate`,
`build-preview` (+ result/logs), `pipelines/{run_id}/reverify`.

**policy-controller** (internal): `internal/approvals/{run_id}`,
`internal/manual-rollback/{run_id}`.

**verification-engine** (internal): `POST /verify` — the one endpoint that runs the actual
statistical comparison.

**explainability-service** (internal): `rca`, `citations`, `decision-report`,
`generate-pipeline`, `stage-failure-rca`, `digest/{tenant_id}`.

Every service exposes a live, auto-generated OpenAPI/Swagger doc at `/docs` (FastAPI's default
— no custom code needed) and a `/healthz` health check.

---

## 16. Database — the tables that matter

- **`projects`** wraps a `pipelines` row (`projects.pipeline_id`) — a project's rollouts are
  ordinary `pipeline_executions` rows tagged with `project_id`. This is deliberate: every
  existing consumer of `pipeline_run_id` (verification, audit, actuation) keeps working
  unchanged for a project-triggered run, and there is never a parallel "project run" table.
- **`pipeline_executions`** — one row per run, `status` written once as `'PENDING'` at trigger
  time and **never updated again**. The real, current status lives in **`execution_state.status`**
  (a separate table pipeline-worker owns) — any query reporting a run's status must read
  `COALESCE(execution_state.status, pipeline_executions.status)`, a real trap this project hit
  (every project's success-rate metric showed 0% despite real completed runs, until this was
  found).
- **`verification_records`**, **`audit_ledger`**, **`approvals`**, **`cost_analysis`** — all
  cascade-delete on `pipeline_run_id`, matching `execution_state`/`stage_logs`.
- **`audit_ledger`** — every actuation, HMAC-signed, with `action` constrained by a real CHECK
  constraint (`WEIGHT_UPDATE`, `ROLLBACK`, `PROMOTE`, `SCALE_ZERO`, `APPROVE`, `BLOCK`,
  `RIGHTSIZING`, `GRADUATE`, `BLUE_GREEN_CUTOVER`).
- Row-Level Security is enforced on every tenant-scoped table via a **PERMISSIVE** policy
  (`FOR ALL USING (tenant_id = current_setting('app.active_tenant_id')::uuid)`) — a real trap
  already hit once: a RESTRICTIVE-only policy with no companion PERMISSIVE policy denies every
  row to everyone, not just the wrong tenant.

Alembic migrations (`migrations/versions/0001`–`0015`) are all idempotent
(`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), specifically so a migration can be
applied directly via `psql` as a safe fallback if `alembic upgrade head` ever fights a
long-lived dev database's bookkeeping.

---

## 17. Security model, end to end

1. **Password → token**: bcrypt hash stored, never the password itself. Login issues a 15-minute
   HS256 access token + a 7-day opaque refresh token (Redis allow-list, rotated every use).
2. **Every request**: the access token is verified, tenant ID extracted, and
   `set_config('app.active_tenant_id', ...)` is run on that request's DB session *before* any
   query — so RLS applies automatically, not as an afterthought.
3. **Role checks**: sensitive actions (pause/resume/rollback, policy saves, onboarding) call
   `require_role()` for real, not just trusting whatever role string is embedded in the JWT.
4. **Verdicts**: HMAC-SHA256-signed at creation (verification-engine), signature + freshness
   verified before policy-controller ever evaluates or acts on one.
5. **GitHub webhooks**: HMAC-verified against GitHub's own `X-Hub-Signature-256`.
6. **OAuth callback**: deliberately unauthenticated (a top-level browser redirect from GitHub
   carries no `Authorization` header) — identity instead comes from a single-use `state` value,
   which doubles as CSRF protection.
7. **Secrets**: a private repo's clone uses the server-side `GITHUB_TOKEN` on pipeline-worker,
   never the user's own OAuth token — deliberately never written into a pipeline's stored YAML,
   which would put a live secret in the database.

---

## 18. What's honestly still missing or deprioritized

- `tests/e2e/*.py` — named explicitly in the original spec, genuinely not built yet.
- A shared `DeploymentTarget` interface unifying Kubernetes and AWS ECS — both targets are
  real and independently working, just not behind one common abstraction.
- Kubernetes/EKS is a real, working, previously live-verified path — but a deliberate
  2026-09-16 decision made AWS ECS the sole *active* build focus going forward, trading the
  assignment's named Kubernetes/Gateway-API rubric line for a simpler single-target backlog.
  The Kind/EKS code was not deleted.
- AI right-sizing recommendations: the real computation and the real CloudWatch data source
  both exist and are both tested — nothing yet wires the second into the first and persists
  the result, so the UI honestly shows "not enough usage data yet" rather than fabricating one.
- A resume-after-crash edge case in the blue-green graduate path (a `None` environment-variable
  value can reach a boto3 call if a pipeline resumes mid-stage after a worker restart) — found
  live during this session's rollback drill, not yet fixed.

---

## 19. Glossary

- **Cohort** — "baseline" (the currently-live version) or "canary" (the new one being
  evaluated) — the unit telemetry and traffic weighting are always split by.
- **Verdict** — the signed, immutable output of a verification run: `HEALTHY` / `DEGRADED` /
  `FAILED` (or `UNVERIFIABLE`, meaning "not enough evidence yet, wait — never guess"), with a
  confidence value and per-metric evidence attached.
- **Blue-green** — atomic 100%-at-once cutover, health-gated, no statistics — for when there's
  no traffic to statistically compare against yet.
- **Canary** — progressive traffic-weight ramp (e.g. 10% → 25% → 50% → 100%), each step
  statistically re-verified before advancing.
- **Actuation** — any real infrastructure mutation policy-controller performs after OPA
  approves it: a weight change, a rollback, a graduation.
- **RLS** — Postgres Row-Level Security; the mechanism enforcing multi-tenant data isolation.
- **Gate 1 / Gate 2** — the two-stage autonomous update loop: Gate 1 is a build+test dry run
  before any real deploy is attempted; Gate 2 is the full existing verification/OPA/actuation
  chain, reached identically whether a run was human-triggered or webhook-triggered.
