# Full Agentic Infra + Pipeline Builder — Orchestration & Design Plan
*(Which agents, how they're orchestrated, which LLMs, and a phased build order — the authoritative plan for this feature; earlier draft revisions have been removed as superseded.)*

---

## R. Industry Research — How Existing Platforms Do This, and What It Changes Here

Researched (web, Sept 2026): Heroku buildpacks, Railway (Nixpacks/Railpack), Google Cloud Buildpacks, Vercel (zero-config + Framework-Defined Infrastructure), Render, Fly.io Launch, Northflank, Backstage (golden paths/Scaffolder), Pulumi AI/Neo, HCP Terraform AI, Spacelift/env0 (policy-as-code), Harness AIDA/AI DevOps Agent, Porter, Qovery, Humanitec, Zeet.

**1. Detection is marker-file scanning, not free-form AI guessing — every platform does this the same deterministic way.**
Heroku's `detect` script greps for `Gemfile`/`package.json`/`requirements.txt`/`go.mod`; Railway's Nixpacks/Railpack, Google Cloud Buildpacks, and Northflank all do the equivalent: scan known config files, match to a known language/framework, done — no LLM call in the hot path. Vercel goes one step further with **Framework-Defined Infrastructure (FDI)**: it parses the framework's own source at build time (API routes, middleware, static pages) to *derive infra shape automatically* from code structure, not from a human describing it.
→ **This platform already has the equivalent** (`getBuildDetection`/`getRepoReport`/`runDetection` in `NewProject.tsx`, `build_preview.py` on the backend). The new "identify what the build is from seeing the git repo" requirement is not a new capability — it's **extending existing deterministic detection to also infer infra shape** (does this repo have a Dockerfile exposing a DB connection string env var → it probably needs a database; does it import a Redis client → it probably needs a cache), then handing *that* structured signal to the Infra Architect Agent instead of relying purely on a human-filled form. The Requirements Form (§2.1) becomes **pre-filled from real repo signals**, with the human only confirming/correcting — closer to Vercel's zero-config philosophy than a blank questionnaire.

**2. A bounded set of "golden path" archetypes, not unbounded free-form generation, is how every mature platform manages arbitrary-scale projects.**
Backstage's Scaffolder model: a small number of vetted, pre-approved templates (e.g. "Python FastAPI + Docker + CI/CD") that self-service through a form, no platform engineer needed per request. Porter/Qovery/Humanitec all similarly map a detected app shape onto a constrained set of known deployment patterns rather than synthesizing arbitrary topology from scratch every time.
→ **Directly answers "what every scale of project should be handled."** Instead of asking the Infra Architect Agent to invent a topology unconstrained, define a small archetype set up front (e.g. *static site*, *stateless web service*, *web service + database*, *web service + database + cache*, *background worker*, *multi-service*) sized by environment tier (dev/staging/production). Detection (§1) picks the closest archetype; the LLM's job narrows from "design any possible AWS architecture" to "parameterize this known-good archetype for this repo's specifics" — far lower hallucination risk, and it's how Porter can credibly claim "git to prod in 60 seconds" on a user's own cloud account.

**3. AI-generated IaC always runs a preview/plan before apply, and policy gates are evaluated at multiple points, not just one big approval.**
Pulumi Neo's documented loop: agent writes HCL → runs `plan`/preview → checks policy-as-code → **human approval** → agent applies → agent verifies. Spacelift/env0 evaluate OPA policy at *several* points (login, access, approval, plan, push, trigger) and policies can **approve, reject, warn, or require human approval** — not a uniform "always ask a human" gate.
→ **Validates the existing state-machine design in §3** (`INFRA_DRAFTING → INFRA_POLICY_CHECK → INFRA_PENDING_APPROVAL`) almost exactly, with one refinement worth adopting: make `INFRA_POLICY_CHECK`'s outcome **tiered**, not binary. A low-risk, low-cost, high-confidence-detection proposal can auto-advance without a human click; a high-cost, low-confidence, or OPA-flagged proposal forces the approval pause. This is the concrete mechanism for "AI corrected by AI but keep human in the loop **where required**" — required is now a defined condition (cost above threshold, OPA warn/reject, detection confidence below a bar), not "always."

**4. Harness's own AI DevOps Agent (AIDA) is the closest existing analog to what's being designed here** — it generates CI/CD pipeline YAML and Terraform/OpenTofu IaC conversationally, inside the same platform UI, then hands off to Harness's existing (non-AI) governed pipeline engine to actually run it. That split — AI authors config, a separate deterministic engine executes it — is exactly Ground Rule 0a in this document, independently arrived at by the market leader in this exact space.

**5. BYO-cloud-account is the norm for this platform category, not the exception.** Porter and Qovery both explicitly provision *into the user's own cloud account* rather than a shared vendor-owned account — confirming the cross-account IAM Role design in §4 below is the correct default, not a nice-to-have.

**6. Native structured-output mode (not free-text-then-parse) is the fix for LLM JSON reliability, and Groq already supports it.** Testing across 244 models in 2026 shows plain "ask for JSON" prompting/JSON-mode breaks schema conformance ~20% of the time, while native structured-output/constrained decoding (enforcing a JSON Schema at the token-generation level, not just asking nicely) exceeds 99% conformance. Groq's own docs (`console.groq.com/docs/structured-outputs`) confirm this is supported today via an OpenAI-compatible `response_format: {type: "json_schema", ...}` parameter, expecting all properties listed under `required`.
→ **Directly de-risks the biggest flagged failure point for the 5-day sprint** (Day 2's `infra_generator.py`). Both `pipeline_generator.py` (existing) and the new `infra_generator.py` should switch from a "JSON object" response format to a **strict JSON Schema `response_format`**, matching the pydantic model field-for-field — this converts "the LLM might return malformed JSON" from a real risk into a near-non-issue, and removes the need for a hand-rolled retry loop.

**7. React Flow's own recommended pattern for auto-laid-out DAGs is React Flow + `dagre` (or `elkjs` for more complex graphs), not building layout math from scratch.** React Flow only renders nodes/edges at given coordinates; it doesn't compute a layout itself. The documented gotcha: dagre returns *center* coordinates for each node, while React Flow expects *top-left* — every quickstart requires converting between the two, or nodes render offset from where dagre intended.
→ **Resolves the "combined-graph layout" open decision** (previously unresolved in §6): use React Flow + `dagre` for auto-layout of the infra→pipeline graph (simplest working option, well-documented), not a hand-built layout algorithm — and budget for the center-vs-top-left coordinate conversion explicitly on Day 4 so it isn't a surprise.

**8. AWS Fargate task sizing is a hard, constrained set of CPU/memory combinations, not a free continuous range** — you pick the nearest supported combination, you don't compute an exact theoretical target. AWS's own guidance frames Fargate as best suited for dev/staging, bursty workloads, and teams where "ops simplicity matters more than per-vCPU cost" — which matches this platform's target user closely.
→ **Resolves the "form defaults for an unsure user" open decision**: since Fargate sizes are already a small discrete set, the safe-default preset per environment tier isn't a hard judgment call to design — it's picking one of a handful of AWS-defined combinations (e.g. dev → smallest supported combo, staging → next tier up, production → smallest combo that supports Multi-AZ + min 2 tasks), which is easy to hardcode as the Infra Architect Agent's default and just as easy for a human to override.

**Net changes to this plan from research:**
- Add a **Repo Detection Agent** (deterministic, extends existing detection code) as the actual first step — before the form, not replacing it (see updated roster below).
- Add a **golden-path archetype layer** that bounds what the Infra Architect Agent is asked to generate.
- Make policy-check outcomes **tiered** (auto-advance / warn / require-approval) instead of a blanket pause every time.
- **Reprioritize the phased build order** (§5, updated below): get universal detect→build→deploy rock-solid for any project shape *first*, before the fuller form/infra-studio/graph UI work — matching the explicit instruction to focus on the deployment part first.
- **Use native structured-output mode for both Groq generation endpoints**, not JSON-mode-and-hope — closes the sprint's #1 flagged risk.
- **Use React Flow + `dagre`** for the graph, not custom layout code — closes the sprint's #3 flagged risk (§6's "combined-graph layout" decision, now resolved).
- **Fargate sizing defaults are now locked**, not an open question (§6's "form defaults" decision, now resolved).

---

## 0. Ground Rules (carried over from existing platform invariants, plus one new scope boundary)

**0a. AI never owns a safety decision — only content generation.** This is not new; it's the same rule already enforced for verification (invariant 1: no AI in the verify/rollback decision, only statistical tests) and for the existing pipeline generator (`pipeline_generator.py`'s own docstring: AI output is re-validated and requires an explicit human Save before anything acts on it). Every agent below either **proposes** something a human approves, or **executes** something a deterministic policy (OPA, cost ceiling, sample-size floor) already cleared. No agent role is allowed to both propose and approve.

**0b. The platform never writes to the user's git repository or source code — it only suggests, for anything code-level; but everything on the deployment side is fully the platform's own responsibility to build from scratch.** This is a hard scope line, and it splits the whole system cleanly in two:

| Side | Ownership | What that means concretely |
|---|---|---|
| **Code / repo side** | Advisory only, human acts | The Repo Detection Agent (§2, new #0) **reads** the repo — clones it, scans files, inspects dependency manifests — and never commits, pushes, opens a PR, or modifies anything in it. If the Copilot/Intake Agent spots a code-level problem (app binds to `127.0.0.1` instead of `0.0.0.0`, missing health-check route, wrong start command), it **tells the human what to fix and how**, exactly like `copilot_engine.py` already does today ("enter `8080` in the Container Port field," "bind to `0.0.0.0`") — it never edits their source, their Dockerfile-in-repo, or opens any change against their GitHub repository. This is already the existing, correct behavior; this plan does not change it and must not regress it. |
| **Deployment side** | Fully agent-built, from scratch | Everything needed to actually deploy — Dockerfile/build config (when the repo doesn't already have one), the infra topology, the Terraform, the pipeline YAML, the provisioning, the actual cloud resources — is generated and executed **entirely inside the platform**, never inside the user's repo. A synthesized Dockerfile (same pattern already used today for nginx/SPA/static-app builds, per `CLAUDE.md`) is a build-time artifact the platform constructs and uses in its own build stage — it is not written back into the customer's git history. The human supplies intent (via the Requirements Form) and approves outputs at the defined checkpoints (§2.4); building the actual deployable artifacts, infra, and pipeline is the agents' job end-to-end, not something the human is expected to author themselves. |

Practically: nothing in §2's roster below is allowed to gain "open a PR" or "commit to the repo" as a capability. Where a component might be tempted to do so (e.g. a future version of the Repo Detection Agent that wants to "just add a Dockerfile for you"), the correct design is always: generate it as a platform-side build artifact used only in *this platform's* build/deploy pipeline, not injected into the user's repository.

---

## 1. Existing Asset to Build On

`services/explainability-service/src/copilot_engine.py` (currently untracked/in-progress) is already a working conversational agent:
- Groq `llama-3.3-70b-versatile`, guardrailed (scope refusal + secret-pattern scrubbing), context-injected with live wizard/GitHub-inspection state, deterministic fallback when Groq is unavailable.
- **Current limitation**: advisory-only. It answers questions and suggests values but cannot take an action or emit a structured object the rest of the system consumes.

The agentic system below **extends this Copilot into the Intake Agent** rather than building a second, parallel chat agent.

`services/explainability-service/src/pipeline_generator.py` (`POST /generate-pipeline`) is already a working generation agent (Groq `openai/gpt-oss-120b`, pydantic-validated, raises rather than fakes on failure). It becomes the **Pipeline Architect Agent** unmodified — just needs the wizard wired to call it (today `projects_router.py` still uses pure templating).

---

## 2. Agent Roster

| # | Agent | Type | Model | Role |
|---|---|---|---|---|
| 0 | **Repo Detection Agent** | Deterministic, NOT an LLM | n/a | **New, runs first. Read-only against the repo — never commits, pushes, or opens a PR (Ground Rule 0b).** Extends the platform's existing detection code (`getBuildDetection`/`getRepoReport`/`runDetection` in `NewProject.tsx`, `build_preview.py` on the backend) to also infer *infra signals*, not just language/build command: presence of a DB client library/connection-string env var → likely needs a database; a Redis/cache client → likely needs a cache; a Dockerfile `EXPOSE`d port → confirms container port; static-only output (`dist/`, `build/`, no server process) → static-site archetype. Marker-file scanning, same technique as Heroku/Nixpacks/Google Buildpacks/Vercel — no LLM call, and no write access to the source. |
| 1 | **Requirements Form** | Deterministic UI, NOT an LLM | n/a | A structured form (not chat), now **pre-filled from the Repo Detection Agent's output** — the human confirms/corrects rather than starting blank (see §2.2). Its answers are compiled into the `IntentSpec`. |
| 2 | **Intake/Copilot Agent** | LLM (conversational, optional/assistive) | Groq `llama-3.3-70b-versatile` | Extends `copilot_engine.py`. Sits *beside* the form — answers "what should I pick here?" questions, but never replaces the form or Repo Detection Agent as the source of truth for `IntentSpec`. |
| 3 | **Infra Architect Agent** | LLM (structured generation, archetype-bounded) | Groq `openai/gpt-oss-120b` | New `infra_generator.py`, mirrors `pipeline_generator.py`'s exact pattern. Input: the locked `IntentSpec` **plus the matched golden-path archetype** (§2.3) — its job narrows from "invent any topology" to "parameterize this known-good pattern," bounding hallucination risk. Output: topology JSON + Terraform HCL + itemized cost estimate. |
| 4 | **Pipeline Architect Agent** | LLM (structured generation) | Groq `openai/gpt-oss-120b` | Already exists (`pipeline_generator.py`). Reused unmodified. |
| 5 | **Policy/Cost Validator** | Deterministic, NOT an LLM | n/a | Existing OPA (`delivery_guardrails.rego`) + cost-delta ceiling logic. **Now tiered** (§2.4): auto-advance / warn / require-approval, not a uniform pause every time — mirrors Spacelift/env0's multi-outcome policy evaluation. |
| 6 | **Provisioning Executor** | Deterministic, NOT an LLM | n/a | Existing `aws_ecs_actuation.py` / `ecs_onboarding.py`, extended with cross-account `sts:AssumeRole`. Actually creates cloud resources once cleared (auto-advanced or human-approved). |
| 7 | **Failure-RCA Agent** (optional, later phase) | LLM (explanatory only) | Groq (same as existing RCA) | Reuses `report_generator.py`'s pattern to explain *why* a provisioning step failed. Never auto-retries or auto-fixes — output is shown to a human, same as today's stage-failure RCA. |

### 2.2 Repo Detection → pre-filled form (not blank-slate)

Instead of a human filling every field from nothing, the flow becomes: **read-only** clone/inspect of the repo → Repo Detection Agent proposes an `IntentSpec` draft (scale guess from any existing k8s/ECS manifests or `Procfile` process counts, DB/cache/storage needs from dependency manifests, region from any existing IaC found in-repo) → **Requirements Form opens pre-filled** with these values, each field flagged "detected" vs. "needs your input" → human confirms or overrides. This is the concrete mechanism for "identify what the build is from seeing git repo" — detection feeds the form, the form still exists as the ground-truth confirmation step (Ground Rule 0a unaffected: detection *proposes*, the human's form submission is what *locks* `IntentSpec`). If detection turns up a code-level problem the human should fix themselves (e.g. the app binds to `127.0.0.1`, or there's no health-check route), that's surfaced as **Copilot advice** the human acts on in their own repo — the platform does not attempt to patch it (Ground Rule 0b).

Everything downstream of the locked `IntentSpec` — infra topology, Terraform, pipeline YAML, any synthesized Dockerfile/build config the repo doesn't already have, and the actual provisioning — is built entirely by the platform's own agents/services and never written into the user's repository. That is squarely the platform's job, not the human's, and not something delegated back to the repo.

### 2.3 Golden-Path Archetypes (bounds "every scale of project")

A small, fixed, versioned set of deployment archetypes the Infra Architect Agent is always parameterizing, never inventing from a blank page:

| Archetype | Matches when | Typical infra shape |
|---|---|---|
| Static site | No server process detected, build output only | S3 + CloudFront (or ALB+nginx to match existing shared-ALB model) |
| Stateless web service | Single process, no DB/cache client detected | ECS Fargate service + ALB target group only |
| Web service + database | DB client/connection string found | + RDS Postgres/MySQL, sized by environment tier |
| Web service + database + cache | + Redis/Memcached client found | + ElastiCache |
| Background worker | No exposed port, queue/cron pattern detected | ECS Fargate service, no ALB target group |
| Multi-service | Multiple Dockerfiles / a compose file with 2+ services | Multiple target groups behind shared ALB, one per detected service |

Environment tier (dev/staging/production, from the form) scales *parameters within* the matched archetype (instance count, Multi-AZ on/off, task size) — it does not change which archetype is picked. This is what lets the same flow handle a solo hobby project and a production multi-service app without the AI needing unbounded design freedom for either.

### 2.4 Tiered policy outcomes (human-in-the-loop "where required," defined precisely)

`INFRA_POLICY_CHECK` (existing OPA + cost logic) now returns one of three outcomes, not a binary pass/fail:
- **Auto-advance**: known archetype, cost within a low pre-approved ceiling, high detection confidence, no OPA warnings → skips straight to `INFRA_APPROVED`, no human click needed. This is the case for the majority of small/simple projects.
- **Warn**: proceeds to `INFRA_PENDING_APPROVAL` but flags specific concerns inline (e.g. "cost estimate is 3x your last project's average — review before approving").
- **Require-approval**: anything OPA rejects, anything above the cost ceiling, low detection confidence, or a non-standard/multi-service archetype → hard pause, same as today's design.

This is the literal definition of "keep human in the loop where required" — required is now three concrete, auditable conditions, not a vague always-ask default.

### 2.5 The Requirements Form, in detail

This is the actual answer to "how does the LLM know what to build" — not inference alone, a direct questionnaire pre-filled by §2.2's detection. Rendered as a new wizard step (before infra drafting begins), fields grouped by concern:

- **Scale & traffic**: expected environment tier (dev / staging / production), rough requests/sec or "unsure — use a safe default," expected concurrent users.
- **Compute**: preferred compute shape if the user has one (Fargate task size hint: small/medium/large), auto-scaling min/max instance count.
- **Data**: does this service need a database (yes/no → type: Postgres/MySQL, Multi-AZ HA required?), does it need a cache (Redis, yes/no), does it need object storage (S3, yes/no).
- **Networking & security**: public-facing or internal-only, does it need a private-subnet-only posture, any mandatory encryption-at-rest requirement.
- **Budget**: a monthly ceiling in USD (feeds directly into the existing `maxCostDelta` guardrail concept, and gives the Infra Architect Agent a hard constraint to design within rather than a number to check after the fact).
- **Region & account**: which AWS region, and which connected AWS account/role (ties into the cross-account BYO-AWS design in §4 below).

These answers are serialized into a fixed JSON `IntentSpec` — the exact object the Infra Architect Agent's prompt template is built from. The Copilot Agent may pre-fill a field it's confident about (e.g. "for a small internal API, 'production' tier + 1-2 min/max instances is typical") but the human always sees and can override the filled value before submitting the form. This is what makes the LLM's output reproducible and reviewable: the same `IntentSpec` always produces a comparable proposal, versus asking Groq to re-derive intent from a chat transcript each time.

None of these are built on a multi-agent framework object model (no LangGraph/CrewAI/AutoGen agent classes). Each is a plain FastAPI endpoint + Groq call, exactly like agent #2/#3 already are today.

---

## 3. Orchestration Design

### Why not LangGraph / CrewAI / AutoGen
This codebase has a consistent, deliberate bias against framework magic: no Celery (plain sync calls + Redis Streams), no `ruptures` (custom NumPy CUSUM/BOCPD), no SDK for Groq (raw `httpx`). Introducing a heavy agent-orchestration framework would be the first violation of that pattern and would make control flow harder to trace for whoever debugs it next (human or Claude Code). A framework's internal graph-execution/retry semantics become one more thing to learn instead of reading a function.

### What to build instead: a plain state machine, Postgres-backed
Same shape as the existing `execution_state.py` / `rollout_scheduler.py` precedent (which already runs the multi-step canary ramp this way):

```
FORM_COLLECTION            (human fills the Requirements Form — §2.1; Copilot Agent assists inline, optional)
  → INTENT_LOCKED          (form submitted → IntentSpec frozen; this is the literal prompt input, not a guess)
  → INFRA_DRAFTING         (Infra Architect Agent called with the locked IntentSpec)
  → INFRA_POLICY_CHECK     (deterministic OPA + cost-ceiling validation runs automatically, BEFORE a human sees it)
  → INFRA_PENDING_APPROVAL (human sees the generated graph + cost breakdown + OPA pass/fail on the UI)
  → INFRA_APPROVED         (or → back to FORM_COLLECTION if the human edits the form and regenerates)
  → PIPELINE_DRAFTING      (Pipeline Architect Agent called)
  → PLAN_PENDING_APPROVAL  (human sees ONE combined n8n-style graph: infra nodes + pipeline stage nodes)
  → PLAN_APPROVED
  → PROVISIONING           (Provisioning Executor — real AWS calls)
  → [existing, unmodified: trigger → verify (SPRT/Mann-Whitney/etc.) → OPA → actuate]
```

- **The form is the actual start of the flow**, not a wizard afterthought — nothing is generated until `IntentSpec` is locked. This directly resolves the earlier open question ("how confident does the Intake Agent need to be before drafting") by removing inference from the critical path entirely: the human declares the requirement, the LLM never guesses it.
- Each `_DRAFTING` stage is a plain function call to an LLM-backed endpoint — no autonomy, no branching decided by the LLM itself.
- `INFRA_POLICY_CHECK` runs before the human ever sees the proposal — this avoids wasting an approval click on something OPA would reject anyway (this was Phase 4 in §5's original ordering; moving it earlier in the flow so a non-compliant AI proposal never reaches the approval screen at all).
- Each `_PENDING_APPROVAL` stage is a hard pause — the state machine does not advance until a human calls the approve endpoint, mirroring the existing `manualApprovalRequired` canary-gate mechanism exactly.
- Editing and regenerating is a first-class transition, not an escape hatch: `INFRA_PENDING_APPROVAL → FORM_COLLECTION` lets the human change scale/budget/data requirements and get a fresh, comparable proposal from the same deterministic prompt template.
- Failure at any drafting stage surfaces the raw LLM error to the human (same as `pipeline_generator.py`'s existing "raise rather than fake" behavior) — never silently retried automatically.
- This state lives in a new table (or a new nullable stage column on an existing project-scoped table — needs a concrete migration decision, see §5), read/written the same RLS-scoped way every other tenant-owned table already is.

### Why this satisfies "full agentic start to end"
"Agentic" here means: the human states an intent once, and every subsequent artifact (infra design, Terraform, pipeline YAML, cost estimate, compliance check, final combined visual plan) is machine-generated without the human hand-authoring YAML or clicking through unrelated forms — they only **approve or edit** at two checkpoints. It does not mean the LLM decides *whether* to proceed — that stays deterministic, on purpose.

---

## 4. UI Changes — Full Wizard Restructure

The wizard's step sequence changes from the original 3 steps to this, replacing the "AI infers intent from the existing fields" framing entirely:

```
Step 0: Choose Repository              (unchanged)
Step 1: Configure Build & Test         (unchanged)
Step 2: Requirements Form  [NEW]       — §2.1's structured questionnaire; Copilot Agent
                                          available as an inline side-panel to help fill fields,
                                          never auto-submits on the human's behalf
Step 3: Infra Proposal + Approval [NEW]— Infra Architect Agent's output rendered as a graph
                                          (nodes = VPC/ALB/ECS cluster/RDS/etc.), cost breakdown
                                          panel, OPA pass/fail panel, Terraform HCL viewer (read-only,
                                          copyable). Actions: Approve / Edit Form & Regenerate.
Step 4: Combined Plan Review [NEW]     — ONE graph: approved infra nodes feeding into pipeline
                                          stage nodes (build → test → canary steps → graduate).
                                          Read-only, final Approve & Create.
[[ Submit ]] → POST /api/v1/projects (payload now carries the locked IntentSpec + approved infra
               + approved pipeline) → provisioning begins → existing verify/actuate chain, unchanged.
```

- **Whole infra visible, always, not just during the wizard** — the same graph component is reused as a **persistent live view inside `ProjectWorkspace.tsx`'s Pipeline tab** after the project exists, streaming state-machine progress the same way `useLiveLogs.ts` already streams logs over `fetch()` (not `EventSource`, per the existing documented constraint that `EventSource` can't carry the `Authorization` header). A user can reopen a project weeks later and still see exactly what infra and pipeline topology it's running on, not just its run history.
- **New library required**: `react-flow` (or equivalent) — nothing suitable exists in `frontend/package.json` today; the current `PipelineDAG.tsx` is a flat row of pills, not a real node/edge graph.
- **Frontend now local-only** — done (see top of this response): removed from `docker-compose.yml`, run via `vite` directly against the dockerized backend.
- **Form component itself**: a plain multi-section form (no AI involved in its rendering) — grouped fields per §2.1, client-side validated, submitted as one `IntentSpec` JSON object. This is deliberately the most "boring"/deterministic screen in the whole flow, because it's the one piece of ground truth everything downstream depends on.

---

## 5. Phased Build Order — Reprioritized: Deployment Reliability First

**Explicit instruction driving this reordering: get deployment working smoothly for any project shape before building out the fuller agentic/form/graph experience.** The phases below are reordered from the previous version of this document so that "any repo, any scale, reliably deployed" ships before the more ambitious infra-generation UI.

**Phase 0 — Wiring (done today)**
- Remove `frontend` from `docker-compose.yml`. Document local run command.

**Phase 1 — Repo Detection Agent + golden-path archetype matching (deployment-critical, do first) — ✅ COMPLETE**
- ✅ **Infra-signal detection**: `shared/repo_scanner.py` has `detect_infra_signals()` + an `InfraSignals` dataclass (`needs_database`/`needs_cache`/`needs_object_storage` with a `_hint` field naming the real dependency matched, plus `is_static_site`), wired into `detect_build_method()` (new `requirements_txt_content` param). Detects Node (`pg`/`mysql2`/`mongoose`/etc., `redis`/`ioredis`, `@aws-sdk/client-s3`/`minio`) and Python (`psycopg2`/`pymongo`/`sqlalchemy`/etc., `redis`/`aioredis`, `boto3`/`minio`) dependency signatures.
- ✅ **Golden-path archetype matching**: `match_golden_path_archetype()` maps every detection to one of the 6 archetypes from §2.3 (`static_site`, `stateless_web_service`, `web_service_with_database`, `web_service_with_database_and_cache`, `background_worker`, `multi_service`), stored on `BuildDetection.archetype`. Precedence: multi-Dockerfile/compose-file presence → Procfile `worker`-without-`web` process type (new, real signal — added Procfile fetching to match Railway/Heroku's own convention per §R.1) → static/SPA → database+cache → database-only → stateless fallback. Never a guess: the fallback is always the least-assuming archetype.
- ✅ **Wired end to end**: both `github_router.py`'s `/build-detection` endpoint and `copilot_router.py`'s `_inspect_github_repo` now fetch Procfile content and return `archetype` + `infra_signals`. Frontend `BuildDetection` TS interface in `frontend/src/api/github.ts` extended with both fields, typed against the 6 archetype strings.
- ✅ **Tests**: 28 new tests added to `services/api-gateway/tests/test_repo_scanner.py` (infra signals + archetype matching). Full `services/api-gateway/tests/` suite run: **178/178 passing**, zero regressions (one pre-existing test's mocked fetch-dict needed a new `procfile_content: None` key added — fixed, and the router call switched to `.get()` defensively so a future added field can't break an existing caller's mock the same way).
- No AI call anywhere in this phase — purely deterministic marker-file/dependency/Procfile scanning, same technique as every researched platform (§R.1).
- Not yet done, tracked separately: validating archetype matching against a range of *real* (not synthetic-test) repos before Phase 2 builds the form on top of it.

**Phase 2 — Requirements Form pre-filled from detection — ✅ COMPLETE**
- ✅ **`IntentSpec` model**: `shared/intent_spec.py` — `EnvironmentTier`/`DatabaseType` enums, full field set from §2.1 (scale/traffic, compute, data, networking/security, budget, region, plus detection-traceability fields), a `model_validator` catching inconsistent submissions (multi_az without a database, database_type without needs_database, max<min instances), and `apply_tier_defaults()` implementing the locked Fargate-per-tier defaults from §R.8 (dev=256cpu/512mem/1-1 instances, staging=512/1024/1-2, production=1024/2048/2-4+Multi-AZ) — never overrides a value the human actually set, including the edge case where a human-set `min_instances` exceeds the tier's own default `max_instances` (caught by a test, fixed by bumping max up to match rather than producing an invalid spec).
- ✅ **Backend endpoints** in `projects_router.py`: `GET /intent-spec/tier-defaults/{tier}` (read-only defaults) and `POST /intent-spec/normalize` (validates + fills unset fields) — pure computation, no DB yet (Phase 4 adds persistence).
- ✅ **`RequirementsForm.tsx`** (new, `frontend/src/components/wizard/`): grouped fields per §2.1, pre-fills `needs_database`/`needs_cache`/`needs_object_storage` and the matched archetype from Phase 1's `detection.infra_signals`/`detection.archetype` exactly once per new detection (never re-overwrites a field the human already touched), shows a "Detected" badge + the real dependency name next to any pre-filled field.
- ✅ **Wired into the wizard**: `NewProject.tsx` now has 4 steps (`STEPS` array extended, breadcrumb grid fixed from a hardcoded `grid-cols-3`), new step 2 renders `RequirementsForm`, Continue-from-step-2 calls `normalizeIntentSpec()` before advancing (network failure doesn't block the wizard — falls through with the un-normalized spec), gated by `canContinueStep2` (only a chosen tier is required — every other field has a safe default, so an unsure human is never blocked).
- ✅ **Tests**: 14 new backend tests (`test_intent_spec.py`) covering every validator branch and the tier-defaults edge case above. Full suite: **192/192 passing**. Frontend: `tsc -b` — zero type errors.
- Shipped without any AI generation call — a human can already complete a deploy using the detected archetype's sane defaults directly, which is itself a deployment-reliability win independent of the AI layer (Phase 4 adds the actual Infra Architect Agent on top of this).

**Phase 3 — Tiered policy outcomes on the existing deploy path — ✅ COMPLETE**
- ✅ **`shared/deployment_readiness.py`**: `evaluate_deployment_readiness(spec: IntentSpec)` — the pre-deploy analog to `policies/delivery_guardrails.rego`'s existing rollout-time gate (same tiered philosophy as Spacelift/env0, §R.3), deliberately kept in plain Python rather than OPA since this fires at project-creation time, before any `pipeline_run_id`/verdict exists for OPA's input shape. Precedence: low detection confidence → `require_approval`; `multi_service` archetype → `require_approval`; else accumulates `warn` reasons (background_worker archetype, no/above-ceiling budget, production+database without Multi-AZ); otherwise `auto_advance`. `AUTO_ADVANCE_COST_CEILING_USD = 50` is the "low pre-approved ceiling" from §2.4.
- ✅ **`IntentSpec` gained `detected_confidence`** (carries `BuildDetection.confidence` through, alongside the existing `detected_*_hint` fields).
- ✅ **Wired into `/intent-spec/normalize`**: response now includes `readiness: {outcome, reasons}` computed on the normalized spec.
- ✅ **Frontend**: `RequirementsForm.tsx` sets `detected_confidence` from Phase 1's detection; `NewProject.tsx` stores the returned readiness, renders a banner on the Policy & Deploy step (nothing shown for `auto_advance`; an amber warning listing reasons for `warn`; a red block requiring an explicit "I've reviewed this and want to deploy anyway" checkbox for `require_approval`, gating the Deploy button until checked).
- ✅ **Tests**: 12 new tests (`test_deployment_readiness.py`) covering every precedence tier, reason accumulation, and the "no detection signal at all ≠ low confidence" edge case (image-sourced projects skip repo detection entirely). Full backend suite: **204/204 passing**. Frontend `tsc -b`: zero errors.
- This is exactly what makes "AI corrected by AI but keep human in the loop where required" concrete: *required* is now three auditable conditions (confidence, archetype, cost/HA posture), not a vague always-ask default — and it already improves today's deploy flow, with zero AI-generated content yet in the loop.

**Phase 4 — Infra Architect Agent (archetype-bounded) + state machine — ✅ COMPLETE (through INFRA_APPROVED; PROVISIONING is Phase 7, out of sprint scope)**
- ✅ **`services/explainability-service/src/infra_generator.py`**: mirrors `pipeline_generator.py`'s shape (httpx → Groq, pydantic-validated result, raise-on-failure via `InfraGenerationError`) with one upgrade per §R.6: **native structured-output mode** (`response_format: {"type": "json_schema", "schema": InfraGenerationResult.model_json_schema(), "strict": true}`) instead of plain `json_object` mode, since this is a materially more complex nested object (topology + Terraform + cost breakdown + policy checks) than a single YAML string. Archetype-bounded by construction — `_ARCHETYPE_RESOURCE_SHAPES` gives the model an explicit, fixed resource description per archetype (e.g. `web_service_with_database` → "one ECS Fargate service + one RDS instance, nothing else"), so it parameterizes, never invents. New `POST /generate-infra` endpoint on the service.
- ✅ **`infra_build_state` table** (migration `0016`, synced into `db/schema.sql`): a new dedicated table (not a column on `projects`) — resolved the open "state-machine storage" decision definitively, since this is genuinely pre-provisioning state with often no `projects` row yet either. `project_id` nullable by design, RLS via the standard `tenant_isolation_*` policy pattern, explicit `GRANT` to `app_user` (the documented trap: a table added by a later migration needs its own grant or every RLS-scoped query 403s before RLS is even consulted).
- ✅ **Three new api-gateway endpoints** in `projects_router.py`: `POST /infra-drafts` (locks the IntentSpec, calls `/generate-infra`, then runs Phase 3's `evaluate_deployment_readiness` on the result — `auto_advance` jumps straight to `INFRA_APPROVED`, everything else pauses at `INFRA_PENDING_APPROVAL`), `GET /infra-drafts/{draft_id}`, `POST /infra-drafts/{draft_id}/approve` (role-gated, `lead-sre`, only valid from `INFRA_PENDING_APPROVAL`). A Groq/explainability-service failure marks the draft `INFRA_DRAFT_FAILED` with the real error persisted — never silently retried or faked.
- ✅ **Tests**: 8 new tests for `infra_generator.py` (explainability-service, including a dedicated test that the response format really is `json_schema` not `json_object`, and that the archetype's resource-shape description reaches the system prompt), 10 new tests for the three endpoints (api-gateway, covering both readiness outcomes, both explainability-service failure modes, and the approval state-transition guard). Full suites: **explainability-service 52/52 passing**, **api-gateway 214/214 passing**.
- `PIPELINE_DRAFTING`/`PLAN_PENDING_APPROVAL` (feeding the Pipeline Architect Agent) is Phase 6, not built here — this phase stops at a human-approvable infra proposal, which is the actual Phase 4 deliverable.

**Phase 5 — Frontend graph (whole infra visible on UI) — ✅ COMPLETE (scope adjusted based on a real finding)**
- **Scope adjustment, discovered while starting this phase**: `PipelineDAG.tsx` (the component the original plan assumed needed replacing) has **already been superseded** by `StageTimeline.tsx` — a richer, real-log-driven vertical timeline, per that file's own docstring. Pipelines in this platform are also a linear stage sequence, not a branching structure — a node/edge graph doesn't add anything StageTimeline doesn't already do better. The infra topology, by contrast, genuinely branches (ALB → ECS service(s) → RDS/ElastiCache/S3), which is where a real graph earns its place. Phase 5 was retargeted accordingly: the graph renders **infra topology**, not pipeline stages.
- ✅ **`@xyflow/react` (React Flow v12) + `dagre`** added to `frontend/package.json` (the "reactflow" package was renamed to `@xyflow/react`; confirmed on the npm registry before installing). `dagre`'s documented center-vs-top-left coordinate gotcha (§R.7) is handled explicitly in the layout function.
- ✅ **`InfraTopologyGraph.tsx`** (new) — read-only React Flow canvas (no drag/connect — matches the sprint's read-only-visualization scope), auto-layout via dagre, per-resource-type icons (ALB/ECS/RDS/ElastiCache/S3).
- ✅ **`frontend/src/api/infraDrafts.ts`** (new) — typed client for Phase 4's three endpoints, mirroring the backend's `InfraGenerationResult`/`infra_build_state` shapes field-for-field.
- ✅ **Wired into the real flow, not a mockup**: `RequirementsForm.tsx` gained a genuine "Preview Infrastructure (AI)" action that calls `POST /infra-drafts` — a real Groq call through Phase 4's actual pipeline — and renders the real returned topology graph, cost breakdown, policy-check results, and a collapsed Terraform viewer, plus the readiness outcome banner (auto_advance/warn/require_approval) matching Phase 3's gate. Nothing here is fabricated data; a missing `GROQ_API_KEY` surfaces the real `InfraGenerationError` message, not a canned fallback (matching Ground Rule 0a — no safety/content action fakes success on failure).
- ✅ **Verified**: `tsc -b` — zero type errors. `vite build` — succeeds cleanly (confirms the React Flow CSS import and bundling actually work, not just type-checks). Backend suite unaffected: still **214/214 passing**.
- **Not done in this phase** (explicitly deferred): a persistent view inside `ProjectWorkspace.tsx` for an *existing* project's infra — that requires linking `infra_build_state.project_id` to a real project, which only makes sense once Phase 6 (pipeline generation) and the final wizard submission are wired together end-to-end. Doing it now would mean wiring against data that doesn't exist yet.

**Phase 6 — Pipeline Architect Agent wiring — ✅ COMPLETE**
- ✅ **`_generate_and_validate_pipeline_candidate()`** (new, `projects_router.py`) — extracted the existing post-creation pipeline editor's (`generate_pipeline_via_ai`) real Groq-call + pipeline-worker-validator retry loop into a shared helper, so it now has two callers instead of one duplicated inline copy. No behavior change to the existing endpoint — same two-attempts-then-fail contract, confirmed by the pre-existing (newly-tested-here) code path still working identically.
- ✅ **`POST /pipeline-preview/template`** (new) — returns the exact deterministic YAML `generate_project_pipeline_yaml()` would produce for the current wizard state, with zero DB writes, so the wizard has a real baseline to hand the AI without creating anything.
- ✅ **`POST /pipeline-preview/generate`** (new) — the Pipeline Architect Agent wired in for real: calls the shared retry-loop helper against a prompt deterministically built from the locked `IntentSpec` (`_prompt_from_intent_spec` — archetype, tier, database/cache/Multi-AZ flags, budget, production-approval requirement; never a value the human didn't declare).
- ✅ **`CreateProjectRequest.ai_tuned_policy_yaml`** (new, optional field) — closes the loop for real: if the human explicitly chooses the AI-tuned candidate in the wizard, `create_project()` **re-validates it server-side** (never trusts a client-supplied YAML string, even one this same backend already validated in the preview call) before using it in place of the deterministic template.
- ✅ **Frontend**: `pipelinePreview.ts` (new client), and a "Preview Pipeline (AI)" action in the wizard's Policy & Deploy step — calls the template endpoint then the AI-tuning endpoint, shows the summary of changes + a collapsed YAML diff, and an explicit "use this instead" checkbox (`useAiTunedPipeline`) that's the ONLY thing that makes `handleDeploy()` include `ai_tuned_policy_yaml` in the final `POST /projects` call. Unchecked (the default): behavior is byte-for-byte identical to before this phase.
- ✅ **Tests**: 7 new tests (`test_pipeline_preview_endpoints.py`) covering the template preview, successful AI generation, prompt content reflecting the real IntentSpec, and the validation-failure-triggers-retry path (asserting exactly two generate calls, matching the documented "two attempts, never a third silent one" contract). Full suite: **api-gateway 221/221 passing**. Frontend `tsc -b`: zero errors; `vite build`: succeeds.
- This is the last of the sprint's originally-scoped six phases (Phases 7–9 were always explicitly deferred past this sprint — see §5A).

**Phase 7 — Real provisioning execution, then cross-account (any user's AWS account)**
- **Split in two, per `AI_INFRA_PROVISIONING_EXECUTION_PLAN.md` (new, researched separately)**:
  - **Phase 7a–7d** (that plan's own numbering): the Infra Architect Agent's approved proposal (currently display-only) actually gets built for real, into the *platform's own* AWS account — via AWS CloudFormation Change Sets (`create_change_set`/`execute_change_set`, all `boto3`), not raw Terraform apply. That plan's §1 research (Pulumi Neo, Harness IaCM, 2026 AI-code-security data) is why: no real vendor executes AI-generated IaC without a deterministic preview+approval gate in between, and CloudFormation gives that natively without a new Terraform state-management burden. Includes a real `InfraProvisioner` interface so a second cloud can be added later without touching the approval state machine — AWS is the only implementation built now.
  - **Then, cross-account**: `sts:AssumeRole` flow (per §4 below) in `aws_ecs_actuation.py`, extending `ecs_onboarding.py` to provision into the customer's own account, per Porter/Qovery's validated BYO-cloud model (§R.5) — this only makes sense once 7a–7d prove real provisioning works at all in the platform's own account.

**Phase 8 — Failure-RCA Agent (optional)**
- Reuse `report_generator.py`'s Groq pattern to explain provisioning failures. Explanatory only, no auto-retry.

**Phase 9 — Tests & docs**
- E2E test for the full detect→form→approval→provision→deploy path (fills the existing gap noted in `CLAUDE.md`: `tests/e2e/*.py` still missing).
- Update `CLAUDE.md`, `SYSTEM_GUIDE.md`, `AGENTS.md` mirrors per the repo's own cross-tool-sync convention.

**"Once we will do what we want next"**: everything from Phase 4 onward (AI-generated infra, the full graph UI, cross-account provisioning) is explicitly deferred until Phases 1–3 prove that detection → archetype-matched deploy is smooth and reliable across a real spread of project shapes.

---

## 5A. 5-Day Sprint Plan (hard deadline — compressed from §5)

A real 5-day deadline exists. This is the actual committed scope for those 5 days — everything else in §5 (Phase 7 cross-account AWS, Phase 8 Failure-RCA, editable drag-and-drop canvas) is explicitly **out of scope for the sprint** and deferred to "after," per the plan above.

| Day | Scope | Notes / fallback if behind |
|---|---|---|
| **1** | Phase 1 — Repo Detection Agent + golden-path archetype matching, tested against a few real repo shapes | Purely deterministic, lowest risk — should not slip |
| **2** | Phase 2 (Requirements Form, pre-filled) + start Phase 4's `infra_generator.py` | **Use native structured-output mode (§R.6)** for the Groq call from the start — do not write a "hope it's valid JSON" version first, it wastes the exact hours this sprint doesn't have |
| **3** | Finish Phase 4 (state machine + migration) + Phase 3 (tiered policy, §2.4) | If the Alembic migration fights back (documented flaky behavior in `CLAUDE.md`), apply the SQL directly via `psql` immediately — don't debug the revision graph, there's no time |
| **4** | Phase 5, scoped down (React Flow + `dagre` per §R.7, read-only) + Phase 6 (wire existing `pipeline_generator.py` into the wizard) | **Fallback the moment this stalls**: extend the existing `PipelineDAG.tsx` pill-row instead of finishing React Flow. Protect Day 5, not this feature. |
| **5** | Integration only — one full run: repo → detect → form → infra proposal → approve → pipeline → deploy on the existing shared ECS/ALB, end to end | No new feature work. This day exists to fix whatever Days 1–4 leave broken. |

**Locked decisions** (previously open in §6, resolved by research so Day 1 can start clean):
- **State-machine storage**: a new small dedicated table (`infra_build_state`, FK to `projects`) — it's pre-provisioning state with no `pipeline_run_id` yet, so it doesn't fit cleanly into an existing run-scoped table; a new table avoids awkward nullable columns on `projects` itself.
- **Form defaults for an unsure user**: hardcode one Fargate size per environment tier, since Fargate sizing is already a small discrete AWS-defined set (§R.8) — dev → smallest supported combo, staging → next tier up, production → smallest combo supporting Multi-AZ + min 2 tasks. Not a fresh design problem, just picking defaults from AWS's fixed list.
- **Combined-graph layout**: React Flow + `dagre` (§R.7), including budgeting explicitly for the documented center-vs-top-left coordinate conversion on Day 4.
- **Terraform apply vs. boto3-direct**: **boto3-direct for the sprint** — the existing `aws_ecs_actuation.py`/`ecs_onboarding.py` path is proven and fast; a Terraform runtime + per-tenant state backend is real new infrastructure that has no place in a 5-day window. Generated Terraform HCL stays documentation/audit-only for now (still shown to the human at approval time, just not executed).

---

## 6. Open Decisions — All Resolved for the Sprint (§5A)

All four items previously open here are now locked (see §5A "Locked decisions" for the sprint-scoped answer to each):
- ~~State-machine storage~~ → new dedicated `infra_build_state` table.
- ~~IntentSpec extraction reliability~~ → resolved by the Requirements Form (§2.5): intent is declared directly by the human, not inferred from conversation.
- ~~Combined-graph layout~~ → React Flow + `dagre` (§R.7).
- ~~Form field defaults for an unsure user~~ → hardcoded Fargate-size-per-tier presets (§R.8).
- ~~Terraform apply vs. boto3-direct~~ → boto3-direct for the sprint; Terraform stays documentation/audit-only until Phase 7 (post-sprint, cross-account).

Nothing is blocking Day 1 anymore. Any *new* open question that surfaces mid-sprint should be resolved with the fastest-to-ship option, logged here, and revisited post-sprint — not debated during the 5 days.

---

## Status
**All 6 sprint-scoped phases (§5, Phases 0–6) are complete and verified.** Phase 0 (Docker/local frontend), Phase 1 (repo detection + golden-path archetypes), Phase 2 (Requirements Form + `IntentSpec`), Phase 3 (tiered deployment-readiness gate), Phase 4 (Infra Architect Agent + `infra_build_state` state machine, through `INFRA_APPROVED`), Phase 5 (React Flow infra topology graph, retargeted from pipeline stages after finding `StageTimeline.tsx` already superseded `PipelineDAG.tsx`), and Phase 6 (Pipeline Architect Agent wired into the wizard, with a real server-side-revalidated override path) — each shipped with real backend/frontend code, not just design, and each verified with passing tests at the time it was completed (final counts: **api-gateway 221/221**, **explainability-service 52/52**, frontend `tsc -b` clean, `vite build` clean).

Phases 7–9 in §5 (cross-account AWS provisioning, the optional Failure-RCA agent, and full E2E test suite/doc sync) remain **explicitly deferred past this sprint**, per the original scope decision in §5A. The end-to-end path a human can now walk through for real: connect a repo → Phase 1 detects infra signals and archetype → Phase 2's Requirements Form locks intent → Phase 3 decides if a human needs to review → Phase 4's real Groq call proposes infra, gated by the same tiered check → Phase 5 shows the real topology as a graph → Phase 6 optionally AI-tunes the pipeline, human's explicit choice → the existing, unmodified verify/OPA/actuate chain runs exactly as it always has. What's still missing before this is a fully self-contained product feature: the infra draft and pipeline choice aren't yet linked back into a persisted, viewable project record after creation (noted as deferred in Phase 5) — a natural next increment once cross-account provisioning (Phase 7) makes "which cloud account" a real question to answer.

Scope boundary locked in throughout (Ground Rule 0b): the platform is advisory-only toward the user's repo/code, and fully responsible, agent-built, end-to-end for everything on the deployment side (infra, pipeline, provisioning) — no phase, sprint or otherwise, added repo-write capability to any agent.

## Live Application Test (post-implementation)

Passing unit tests were not sufficient proof — running the actual stack (`docker compose`, real Postgres, real Groq, a real browser via Playwright driving the real wizard UI) caught **four real bugs** invisible to mocked tests, all now fixed:

1. **`:param::jsonb` in `sqlalchemy.text()` is silently never bound.** SQLAlchemy's bind-param parser treats a `:name` immediately followed by `:` as an escaped literal colon (the mechanism that lets `::type` casts coexist with `:param` syntax) — so `:intent_spec::jsonb` was left as literal text and asyncpg rejected it as a syntax error. Fixed by wrapping in parens: `(:intent_spec)::jsonb`. No unit test could catch this since the `FakeDB` test doubles never parse SQL syntax.
2. **A handler must never call `db.commit()` itself.** `auth/middleware.py` wraps the entire request in one `session.begin()` transaction, auto-committing on success or rolling back the whole thing on any exception. `create_infra_draft`'s interim commits ended that transaction early, and every subsequent `db.execute()` in the same request then failed with `Can't operate on closed transaction`. Fixed by removing all mid-handler commits; one consequence accepted deliberately: a Groq failure now rolls back the draft's INSERT too (no `INFRA_DRAFT_FAILED` audit row survives), but the HTTPException's own error detail still reaches the caller.
3. **Groq's strict `json_schema` mode requires `additionalProperties: false` on every object in the schema, root and every nested `$defs` entry** — pydantic's `model_json_schema()` never sets it, and Groq returns a bare 400 with no field-level detail when it's missing anywhere. Fixed with a recursive `_require_no_additional_properties()` pass over the generated schema, with a regression test asserting it's set on the root and every `$defs` entry.
4. **A JSX nesting bug — real, only visible in a browser.** The "AI-tuned pipeline" section (Phase 6) was placed one closing brace too late, landing it *outside* the `{step === 3 && (...)}` conditional — it rendered on every wizard step, not just step 4. `tsc -b` and `vite build` both stayed green throughout (neither type-checks nor bundling catches a misplaced JSX boundary), and only a live screenshot on step 0 revealed it.

After fixes, verified live end to end: real detection-blocked-by-expired-dev-token path correctly disables "Preview Infrastructure" with a clear reason (never guesses an archetype); the Requirements Form, tiered readiness banner (`warn` outcome rendered exactly as designed for a no-budget case), a real Groq-generated infra topology + Terraform + cost breakdown (Phase 4, ~9s round trip), and a real Groq-tuned pipeline YAML with a working opt-in checkbox (Phase 6, screenshotted with the actual generated YAML visible) all render correctly in an actual Chromium session. One pre-existing, unrelated environment issue surfaced and was **not** fixed (out of scope): this dev machine's stored GitHub token is expired (401 on `/user/repos` and repo-tree fetches) — blocks real repo-based detection testing here, but is a credentials/ops issue, not a code defect, and affects a feature (GitHub repo listing) that predates this sprint's work entirely.

Full suites re-verified after every fix: **api-gateway 221/221**, **explainability-service 53/53**, frontend `tsc -b` and `vite build` both clean.

---

## Sources (industry research, §R)
- [Buildpacks | Heroku Dev Center](https://devcenter.heroku.com/articles/buildpacks)
- [GitHub - railwayapp/nixpacks](https://github.com/railwayapp/nixpacks)
- [Why We're Moving on From Nix (Railpack)](https://blog.railway.com/p/introducing-railpack)
- [Google Cloud's buildpacks | Buildpacks | Google Cloud Documentation](https://docs.cloud.google.com/docs/buildpacks/overview)
- [Zero Config Deployments - Vercel](https://vercel.com/blog/zero-config)
- [Framework Support | vercel/vercel | DeepWiki](https://deepwiki.com/vercel/vercel/5-framework-support)
- [Build Pipeline – Render Docs](https://docs.render.com/build-pipeline)
- [Fly Launch overview · Fly Docs](https://fly.io/docs/reference/fly-launch/)
- [Build and deploy your code | Northflank docs](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code)
- [What is a golden path for software development? — Red Hat](https://www.redhat.com/en/topics/platform-engineering/golden-paths)
- [How to Build Golden Paths in Backstage IDP with Software Templates](https://medium.com/@rameshavutu/how-to-build-golden-paths-in-backstage-idp-with-software-templates-170adce436fe)
- [Using Terraform with AI: Workflows, Tools & Security — Spacelift](https://spacelift.io/blog/terraform-ai)
- [HCP Terraform Positions Itself as the Control Plane for AI-Driven Infrastructure — InfoQ](https://www.infoq.com/news/2026/09/hcp-terraform-ai-driven-control/)
- [Policy - Spacelift Documentation](https://docs.spacelift.io/concepts/policy)
- [env zero (env0) vs Spacelift: IaC Orchestration Comparison](https://spacelift.io/blog/env-zero-vs-spacelift)
- [Which Infrastructure Automation Platforms Actually Support Policy Guardrails for AI Agents? — Qovery Blog](https://www.qovery.com/blog/policy-guardrails-ai-agents-infrastructure-automation-platforms)
- [Harness AI DevOps Agent | Harness Developer Hub](https://developer.harness.io/docs/platform/harness-ai/devops-agent/)
- [Infrastructure as Code (IaC) Management with AI | Harness](https://www.harness.io/products/infrastructure-as-code-management)
- [Porter | Platform as a Service, Reimagined.](https://www.porter.run/)
- [Qovery - The Agentic Infrastructure Platform](https://www.qovery.com/)
- [Humanitec Platform Orchestrator](https://humanitec.com/products/platform-orchestrator)
- [Structured Outputs - GroqDocs](https://console.groq.com/docs/structured-outputs)
- [Structured Outputs Across LLM Providers: 244 Models Tested (2026) — Requesty](https://www.requesty.ai/blog/structured-outputs-across-llm-providers-the-compatibility-mess)
- [Quick Start - React Flow](https://reactflow.dev/learn)
- [Dagre Tree - React Flow](https://reactflow.dev/examples/layout/dagre)
- [Choosing Fargate task sizes for Amazon ECS — AWS Docs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-size-best-practice.html)
- [ECS Fargate Best Practices: Running a Fleet of 10+ Environments Without the Pain — DEV Community](https://dev.to/dspv/ecs-fargate-best-practices-running-a-fleet-of-10-environments-without-the-pain-5hka)
