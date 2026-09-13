# Phase 8 — Render-style Project Workspaces & GitHub-driven Delivery

**Status: done — verified end-to-end against the running stack (real GitHub clone, real Kubernetes provisioning, real signed verdict, real OPA-authorized traffic shift).**

## Goal
A user connects a GitHub repo, configures build/test/verification in a 3-step wizard, and gets an isolated project workspace where every existing screen (pipeline execution, verification inspector, policy editor, audit ledger) is scoped to that one project — modeled on Render's UX.

## The central integration decision (read this before writing any code)

The prompt specifies a `Project` model, a `PipelineRun` model, and a `StageLog` model. Two of those three already exist under different names, and **the entire downstream system is keyed to the existing names**:

| Prompt's model | What already exists | Decision |
|---|---|---|
| `Project` | `pipelines` (tenant_id, name, policy_yaml) — holds the declarative YAML the worker executes | **New `projects` table that OWNS a `pipelines` row** (FK `pipeline_id`). The project holds repo/build/image metadata; its pipeline row holds the generated policy YAML. |
| `PipelineRun` | `pipeline_executions` (pipeline_run_id, status, current_stage, current_traffic_weight) | **Extend the existing table** with `project_id`, `trigger_type`, `commit_sha`, `commit_message`. Do NOT create a second runs table. |
| `StageLog` | nothing durable — logs live only in Redis `logs:{run_id}` with a 24h TTL | **New `stage_logs` table.** |

**Why wrapping rather than replacing:** `verification_records`, `audit_ledger`, `approvals`, `cost_analysis`, and `execution_state` all carry `pipeline_run_id` as a foreign key into `pipeline_executions`. A parallel `pipeline_runs` table would mean the SPRT verdicts, HMAC-signed audit rows, and OPA actuations produced by a project's rollout would land in a table nothing else joins to — the Verification Inspector and Audit Ledger would show nothing for projects. Wrapping means **a project rollout is an ordinary pipeline execution**, so every existing service keeps working untouched.

**Corollary — `verdict` / `confidence` are not denormalized onto the run.** The prompt lists them as run columns, but `verification_records` is already the single source of truth for both (written by verification-engine, HMAC-signed). The projects API joins them into the run response, so the API contract the frontend consumes matches the spec exactly, without a second copy that can drift from the signed record.

**Corollary — no new execution engine.** The worker already supports `build` (with `repoUrl` → real `git clone`, see `tasks/git_clone.py`), `test` (shell command), `deploy`, and `canary_loop` stage types. A project's generated pipeline YAML simply declares all four. Step 6's "clone → build → test → canary" hook is therefore **wiring the wizard's inputs into the generated YAML**, not writing a new runner.

## Deliverables

### 8.1 — Schema (migration `0006`)
- [x] `projects` table: `project_id`, `tenant_id` (RLS), `pipeline_id` FK, `name`, `repo_url`, `branch`, `root_directory`, `dockerfile_path`, `test_command`, `container_image`, `active_production_tag`, `canary_tag`, `status` (`IDLE|BUILDING|TESTING|VERIFYING|HEALTHY|ROLLED_BACK`), `created_at`. RLS PERMISSIVE policy (never `AS RESTRICTIVE` — see CLAUDE.md).
- [x] `pipeline_executions` gains `project_id` (FK, `ON DELETE CASCADE`), `trigger_type`, `commit_sha`, `commit_message` — all nullable so every existing row and every non-project pipeline run stays valid.
- [x] `stage_logs` table: `id`, `run_id` FK, `tenant_id` (RLS), `stage_name`, `content`, `created_at`.
- [x] Mirror all of the above into `db/schema.sql` (the init-time DDL) and `db/models.py` (ORM).

### 8.2 — GitHub integration (`/api/v1/integrations/github`)

**Connection is a real OAuth 2.0 Authorization Code flow** ("Connect GitHub", as Render does it), not a pasted personal access token:
- [x] `GET /status` — whether this user is connected, and as whom. Never returns the token.
- [x] `GET /authorize-url` — mints a single-use random `state`, stores it in Redis bound to this user id, returns GitHub's authorize URL. Scope: `repo read:user` (`repo` is what makes private repositories visible).
- [x] `GET /callback` — GitHub's redirect target, **allowlisted in `auth/middleware.py`** because a top-level browser navigation carries no `Authorization` header. Identity is re-established from the `state`, which is also what makes the flow CSRF-resistant. Always ends in a redirect back to the wizard (never raw JSON) because a browser window is on the other end.
- [x] `POST /disconnect` — forgets the stored token and profile.

**Where the token lives: Redis, never Postgres.** This service has no `cryptography` dependency, so a DB column would mean a plaintext OAuth token at rest in the tenant database. Redis matches how Phase 4 already stores revocable refresh tokens; a Redis restart just means the user reconnects. The token is never logged and never sent to the browser.

**Setup required before "Connect GitHub" works** (the wizard says this on screen when unconfigured, and still offers the Public Git Repository tab meanwhile):
1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.
2. Homepage URL `http://localhost:3000`; Authorization callback URL `http://localhost:8000/api/v1/integrations/github/callback` — this must match `GITHUB_OAUTH_REDIRECT_URI` **exactly** or GitHub returns `redirect_uri_mismatch`.
3. Put the Client ID/Secret in `.env` as `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`, then `docker compose up -d api-gateway`.

**Private-repo caveat (honest scope limit):** OAuth solves repository *discovery and selection*. The *build* still clones using the token named by the generated pipeline's `repoCredentialsEnvVar`, which is `GITHUB_TOKEN` on **pipeline-worker** — the server's token, not the end user's. The user's OAuth token is deliberately not written into pipeline YAML (that would put a secret in the database), so cloning a private repo needs either a server-side `GITHUB_TOKEN` with access, or a follow-up that hands per-project credentials to the worker out of band.

Token resolution order for API calls: `X-GitHub-Token` header → the signed-in user's stored OAuth token → server `GITHUB_TOKEN`.

- [x] `GET /repos` — the connected account's repositories, normalized to `{id, name, full_name, default_branch, private, clone_url, description, owner}`.
- [x] `GET /repos/{owner}/{repo}/branches` — branch list for the wizard's branch selector.
- [x] `GET /repos/{owner}/{repo}/commits/{ref}` — HEAD commit, so a triggered rollout carries real git provenance.
- [x] A missing/invalid token or unconfigured OAuth returns a clear, actionable error (400/503, never a 500).

### 8.3 — Projects API (`/api/v1/projects`)
- [x] `GET /` — every project for the tenant + latest run status/verdict/weight and production version.
- [x] `POST /` — creates the project: generates its pipeline YAML from the wizard payload, registers the `pipelines` row, links both. `lead-sre` role required (matches existing `POST /services`).
- [x] `GET /{project_id}`, `DELETE /{project_id}` (cascades runs).
- [x] `POST /{project_id}/rollout` — creates a `pipeline_executions` row (with `project_id`, `trigger_type`, `commit_sha`) and publishes to the existing `stream:pipeline:start` Redis Stream. Identical mechanism to `POST /pipelines/{id}/runs`.
- [x] `GET /{project_id}/runs`, `GET /{project_id}/runs/{run_id}/stages`, `GET /{project_id}/runs/{run_id}/logs/stream` (SSE), `POST /{project_id}/runs/{run_id}/rollback`.
- [x] Every query filters on `tenant_id` AND goes through `get_request_db` (the RLS-scoped session) — never `get_db`.

### 8.4 — Frontend: Overview (`/projects`)
- [x] Service-card grid: name, repo badge, branch, live status badge (`HEALTHY` green / `CANARY RUNNING` blue pulsing with weight / `FAILED`/`ROLLED_BACK` red), last-deploy line, `Trigger Rollout` quick action.
- [x] Render-style empty state when the tenant has zero projects.
- [x] **Honesty constraint (carried from Phase 1b):** card stats must come from real data. 7-day success rate and MTTV are computable from `pipeline_executions`; **cost delta is NOT** (`cost_analysis` is only written when a cost stage runs) — show it only where a real row exists, never a fabricated number.

### 8.5 — Frontend: 3-step wizard (`/projects/new`)
- [x] Step 1 Choose Repository — live-filtered GitHub repo list, private/public icons, manual clone-URL fallback.
- [x] Step 2 Configure Build & Test — branch selector (from GitHub API), root dir, Dockerfile path, test command, registry image, baseline + canary tags.
- [x] Step 3 Progressive Policy & Deploy — canary preset (10→25→50→100), golden-signal assertions, confidence floor / min sample size / max cost delta guardrails, live YAML preview.

### 8.6 — Frontend: project workspace (`/projects/:id`)
- [x] Sub-header (project name, repo link, prod version) + `Trigger New Rollout` / `Pause` / `Emergency Rollback`.
- [x] Tabs reusing the EXISTING screen components, scoped to this project's pipeline/run: Pipeline View, Verification Inspector, Policy & Gates, Audit Ledger.
- [x] 3-stage stepper (Build → Test → Progressive Canary) with per-stage log filtering and the traffic-weight ramp.

### 8.7 — Execution hook
- [x] Generated pipeline YAML wires the wizard inputs into the worker's existing stage types: `build` (with `repoUrl`/`repoRef`/`dockerfilePath`), `test` (with `command`), `deploy`, `canary_loop`.
- [ ] Worker persists per-stage logs to `stage_logs` at stage completion (Redis stays the live fast path — matching the existing `execution_state` Redis+Postgres pattern).

## Acceptance criteria
- [x] Schema applied cleanly to the running database; existing rows unaffected. **Note:** applied as idempotent DDL inside the postgres container rather than via `alembic upgrade head`, because this database's `alembic_version` table already contained two rows (`0001` and `0003`) — pre-existing inconsistent bookkeeping, untouched here. `0006_add_projects_and_stage_logs.py` and `schema.sql` both carry the same statements for fresh environments.
- [x] A project produces a rollout whose verdict appears in the EXISTING Verification Inspector and whose actuations appear in the EXISTING Audit Ledger — **verified live**: project `e2e-demo` → real verdict `HEALTHY` (confidence 0.857, SPRT LLR −3.04 crossing the accept boundary) → HMAC verified → `OPA:rule=PROMOTE_STEP` → real `WEIGHT_UPDATE` to 25%, all rendered by the unmodified existing screens. The wrap-don't-duplicate decision held.
- [x] Cross-tenant check: tenant B's JWT sees zero projects and gets 404 (not 403 — existence is not leaked) on tenant A's project detail and run log stream.
- [x] `tsc -b` + `vite build` clean; Vitest suite still 12/12.
- [x] Driven live in a browser against the running stack (overview grid, 3-step wizard, workspace with all four tabs).

## Real bugs found by running it, not by reading it
1. **Public repos could never build.** The generated build stage emitted `repoCredentialsEnvVar: GITHUB_TOKEN` unconditionally, and `git_clone.py` (correctly) hard-fails when a pipeline names a credentials env var that is unset — so every public-repo project failed at clone. Fixed by threading GitHub's own `private` flag through as `repo_private` and only emitting the credential line for private repos.
2. **The live log stream had never worked.** `useLiveLogs` used the browser's `EventSource`, which cannot send custom headers, so it never sent `Authorization: Bearer …` and every stream was rejected 401 by `tenant_context_middleware` — in the classic console too, not just here. The "Waiting for log output…" placeholder made a rejected request look like a quiet pipeline. Fixed by streaming over `fetch` (which supports headers); the `?access_token=` alternative was rejected because query strings land in access logs, history and `Referer`.
3. **SSE frames were never parsed.** After (2), `sse_starlette` emits CRLF, so frames are separated by `\r\n\r\n`; the parser split on `\n\n`, matched nothing, and buffered forever. Fixed by normalizing line endings before splitting.
4. **The project Audit Ledger showed every other project's actuations.** The reused `AuditLedger` screen is the one screen scoped by *tenant* rather than by pipeline/run, so inside a workspace it listed all 33 tenant-wide rows. Fixed with a `projectId` on the outlet context that routes it to the project-scoped endpoint. Its SOC 2 export is still tenant-wide, and now says so on the button rather than quietly exporting more than the table shows.

## 8.8 — Retiring the classic `/app` console (follow-up)

The old pipeline console was removed so there is exactly one authenticated UI. Preserving behaviour took more than deleting routes:

- [x] **Every pipeline is now reachable.** Two pipelines registered outside the wizard (`payments-pipeline` — 86 runs/28 verdicts, `checkout-service-rollout` — 27 runs) had no project, so deleting `/app` would have hidden them and their history. Migration `0007` adopts every orphan pipeline into a project and back-fills `project_id` onto its executions (113 rows re-attached; 0 orphans remain).
- [x] **Adopted projects have no invented repo.** `projects.repo_url`/`container_image` became nullable and the UI renders "No repository connected" rather than a fabricated URL.
- [x] **`AppContext` moved** from `layouts/AppLayout.tsx` to `types/app-context.ts` — the contract belongs to the screens that consume it, not to a layout that happened to supply it.
- [x] **`/app/*` redirects to `/projects`** so old links and bookmarks don't 404; login now lands on `/projects`.
- [x] **The wizard absorbed the deleted "Add Service" dialog.** That dialog exposed port / health-check path / traffic path prefix, which the wizard hardcoded — those are now step-2 fields, making the wizard a strict superset before the dialog was removed.
- [x] Playwright specs rewritten for the new routes, plus a new case asserting the `/app` redirect.

**Real bug this surfaced:** the overview's 7-day success rate read `pipeline_executions.status`, which is written once as `PENDING` at trigger time and *never updated* — all 119 rows were `PENDING`, so every project displayed **0% success** regardless of reality. The durable final status lives in `execution_state` (worker-written). Fixed with `COALESCE(execution_state.status, pipeline_executions.status)` in the overview, stats and run-list queries, and in-flight runs excluded from the success-rate denominator. The two adopted pipelines now correctly read 100%, and the two that genuinely failed at build read 0%.

## 8.9 — "Existing Image" source (follow-up)

A third wizard tab, alongside Git Provider and Public Git Repository, for deploying a pre-built image straight into the canary loop — no clone, no build, no test — matching Render's own "Existing Image" source type.

- [x] `CreateProjectRequest.source_type: "repository" | "existing_image"`. When `"existing_image"`, `generate_project_pipeline_yaml()` emits **only** the `canary_verify` stage — the same single-stage shape the two pre-Phase-8 pipelines already used (see 8.8), so this is a pattern the worker has executed correctly since before projects existed.
- [x] **Registry credentials** (`services/api-gateway/src/routers/registry_router.py`, `/api/v1/integrations/registry/credentials`) — stored in Redis, tenant-scoped, no TTL (infrastructure configuration, not a session artifact). The `dockerconfigjson` payload a Kubernetes `Secret` needs is precomputed at write time; a `GET` never returns the secret, only `{id, name, registry, username}`.
- [x] `pipeline-worker` resolves `registry_credential_id` by reading the **same Redis instance** api-gateway wrote to, rather than the raw credential being forwarded through the onboarding HTTP payload — the secret material appears in exactly one request body (the one that created it).
- [x] `manifest_generator.py`'s `ServiceOnboardingSpec` gained `image_pull_secret_name`; both baseline and canary Deployments get `imagePullSecrets` when set. `onboarding.py` creates the real `kubernetes.io/dockerconfigjson` Secret **before** the Deployments that reference it, so a Deployment is never briefly live pointing at a Secret that doesn't exist yet.
- [x] Frontend: `Image URL` + `Credential` picker with inline "Add credential"; a trailing `:tag` is parsed off (`parseImageRef()`, colon-after-last-slash to avoid misreading a registry port as a tag) and pre-fills the canary tag. Step 2 hides branch/root-directory/Dockerfile-path/test-command — none apply — and shows a one-line explanation instead.

**Verified live**, not just compiled: created a project with `container_image: nginx`, `registry_credential_id` set, `provision_cluster: true` → confirmed via `kubectl` (executed inside the `pipeline-worker` container) that the real Secret has `type: kubernetes.io/dockerconfigjson`, the real Deployment's `imagePullSecrets` correctly names it, and the container image resolved to `nginx:1.25` with no tag-doubling.

## 8.10 — Making GitHub and Docker actually work end-to-end (follow-up)

Before this, a repository-sourced project's build stage was disconnected from reality: `build_task.py`/`deploy_task.py` **hardcoded `localhost:5001/payments:{tag}`**, ignoring the pipeline's declared `image:` field entirely (`worker.py` never even read it), and **nothing ever pushed a built image anywhere** — the canary Deployment (created at onboarding with the project's real, correctly-named image) could never pull what a build stage had just produced under a completely different, un-pushed name. Every repository-sourced project's build→deploy chain was silently broken.

- [x] `worker.py`'s build-stage handler now reads `config["image"]` and `config["registryCredentialId"]`, resolves the credential from the same tenant-scoped Redis store `registry_router.py` writes to, and passes both through.
- [x] `build_task.py` tags the real image name and, after a successful build, does exactly one of:
  - **registry credential attached** → real `docker login`-equivalent authenticated `docker push` (`_push_to_registry`, using `docker-py`'s `images.push(..., auth_config=...)`, inspecting every streamed progress line for an `error` key — a failed push completes the HTTP request normally, so a bare "did it throw" check would miss it).
  - **no credential** → `kind_load_image` (`services/pipeline-worker/src/tasks/kind_loader.py`, new) — loads straight into the local Kind cluster's containerd, matching what local development already relies on `make deploy-sample-app` to do by hand.
  - `image_name is None` (the original hand-wired demo pipeline, which declares no `image`) → completely unaffected, byte-for-byte the old hardcoded path.
- [x] **GitHub credential broker**: `projects_router.py`'s `trigger_project_rollout()` copies the triggering user's own connected GitHub OAuth token into a short-lived `clone_token:{run_id}` Redis key (900s TTL) when one exists; `worker.py` checks it first and deletes it right after the clone; `git_clone.py` accepts the token directly, falling back to the existing `credentialsEnvVar` (server-wide `GITHUB_TOKEN`) lookup when absent. A private-repo clone now uses the actual caller's access, not a token every tenant shares.

### `kind_load_image` — reimplementing `kind load docker-image`
No `kind` binary and no `docker` CLI exist in this container (only the SDK, talking to the mounted `/var/run/docker.sock`), so this reimplements the same mechanism the `kind` CLI itself uses: `docker save <image> | docker exec -i <node> ctr --namespace=k8s.io images import -`, via `docker-py`'s low-level `exec_create`/`exec_start(socket=True)` raw duplex socket. Node discovery is by the `io.x-k8s.kind.cluster` label Kind sets on every node container — not a hardcoded cluster name.

**Two real bugs found only by testing this live against the actual 3-node Kind cluster, not by reading the code:**
1. **`put_archive` (docker-py's file-copy-into-container helper) silently does nothing in this environment** — it returns `True` and the target file is never actually written, confirmed even for a trivial one-line text file. Abandoned in favor of the raw exec-socket approach.
2. **The raw-socket approach's first attempt truncated large transfers**: closing the socket immediately after an `except OSError: break` in the drain loop cut the connection before the daemon had finished forwarding the final buffered bytes to the container's actual stdin — `ctr` would report a specific missing content digest, meaning most (not none) of the stream had arrived. Fixed by draining `recv()` until a true empty-bytes EOF (never bailing on the first exception) before closing.

A third finding was environmental, not a bug in this code: a *freshly pulled* multi-arch manifest-list image (tested with `busybox:latest`) fails to import with a "content digest not found" error — a known Docker-Desktop-containerd-image-store interop quirk where the legacy `docker save` tar format doesn't fully embed a manifest-list image's blobs when they're resolved from the content store rather than a single flat layer chain. **This does not affect the real pipeline**: a project's build stage produces a genuinely, freshly *built* single-platform image (`client.images.build(...)`), which is fully self-contained — verified by building a real image locally and loading it cleanly into all 3 nodes.

**Verified fully live, end-to-end, through the real HTTP API** (not a direct function call): created a project against `docker-library/hello-world` (a real public repo, `amd64/Dockerfile`, branch `master`) with `container_image: registry.internal/getting-started-demo` and no registry credential → triggered a real rollout → real `git clone` → real `docker build` → real `kind_load_image` into all 3 nodes → real test stage → real `HEALTHY` verdict (confidence 0.808). Confirmed via `ctr --namespace=k8s.io images ls` on a real node that the loaded image is named **exactly** `registry.internal/getting-started-demo:v1.1.0` — the project's actual configured name, not the old hardcoded one. The registry-push branch was separately verified against a real (ephemeral, for-this-test-only) registry container: correct image/tag/auth_config reached the push call; the registry itself being unreachable via plain HTTP from the Docker daemon's own network namespace (a `localhost` vs. daemon-namespace vs. container-namespace distinction — image push/pull is a *daemon-side* operation, not a client-side one, so a registry only reachable from the calling container's own network namespace is not reachable to the daemon) surfaced as a clear, correctly-handled error rather than a silent failure or a corrupted push. Real TLS-terminated registries (Docker Hub, GHCR, ECR, GCR) need no extra host configuration; a self-hosted plain-HTTP registry needs the operator to add it to the Docker daemon's own `insecure-registries` config — a standard Docker operational requirement, not a gap in this code.

## Known limitations (deliberate, not oversights)
- **Cost delta is not shown on project cards.** `cost_analysis` rows are only written when a cost stage runs, which these pipelines don't have — so there is no real number to display, and a placeholder would be a fabricated metric.
- **`GET /{project_id}/runs/{run_id}/logs/stream`** exists and supports `?stage=` filtering, but the UI reuses the pipeline-scoped log endpoint with client-side stage filtering (instant filter changes, no stream reconnect).
- **`stage_logs` is created and indexed but not yet written to.** Redis remains the live log path; persisting per-stage output at stage completion is the remaining piece of 8.7.

## Depends on
Phases 1–6 (done) and 1b (visual refresh) — this builds the project layer on top of them.
