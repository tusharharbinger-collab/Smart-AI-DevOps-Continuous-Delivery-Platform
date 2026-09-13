# Phase 7 — Deployment Plan (Local → Global)

## Goal
The platform runs reliably for real, unattended, 24/7 use — first for one team on one server, then (if warranted) across multiple regions for many teams.

## Why last
Deploying an unfinished product just moves the unfinished product somewhere else with more people watching. Every earlier phase (real telemetry, real onboarding, real security, real reliability, real observability) needs to be genuinely done first — this phase is about *where it runs*, not *whether it works*.

## Explicit focus: local-first, with a documented path to global — not both at once
Per direction: get this fully solid running locally (`docker compose up`, one command, zero manual steps, works every time) before spending effort on cloud deployment. The plan below is staged so "local, hardened" is a complete, useful checkpoint on its own — global deployment is the *next* checkpoint, not a requirement to reach before local is done.

## Stage A — Local, hardened (do this first)

### A.1 — One-command bootstrap, no manual steps
- [ ] `make setup` should produce a fully working system with zero manual `psql`/`kubectl` commands required afterward — currently, several things needed hand-run SQL during development (seeding demo users, fixing RLS policies, generating the in-cluster kubeconfig). All of these need to become part of the automated bootstrap:
  - Demo user/tenant seeding → an idempotent seed migration or script, not ad hoc `INSERT`s run by hand.
  - The in-cluster kubeconfig generation (`scripts/setup/connect_kind_network.sh`) → folded into `make setup`'s sequence, not a separate manual step someone has to remember.
- [ ] `docker compose up --build` from a completely clean clone, with nothing else run first, should produce a working system. Test this literally — clone into a fresh directory and time how many manual steps it actually takes today vs. the target of zero.

### A.2 — Local reliability
- [ ] `docker-compose.yml` healthchecks and `depends_on: condition: service_healthy` should cover every real dependency — audit for any service that could start before something it needs is ready.
- [ ] A `make reset` that tears down and rebuilds everything from scratch cleanly (currently `make clean` exists but doesn't cover the Kind cluster or generated kubeconfig).
- [ ] Document exact resource requirements (CPU/RAM) for running the full local stack — Kind cluster + 10 docker-compose containers is not lightweight; a new user should know what to expect before attempting it.

### A.3 — CI: GitHub Actions (still "local" in spirit — this runs on every commit, not in production)
**Decided: GitHub Actions**, not a generic "or equivalent" — free for public/small-team private repos, no separate CI infra to run/maintain, and it's the natural place for Stage B's deploy workflow (A.3 → B.2) to live later without switching tools.

Planned workflows (each its own file under `.github/workflows/`):
- [ ] **`test.yml`** — triggers on every push and PR. Jobs, run in parallel where independent:
  - `verification-engine`, `policy-controller`, `pipeline-worker`, `explainability-service` test suites (each `python -m pytest tests/` from its own service directory, matching how they're run today).
  - `opa test policies/ -v`.
  - `tests/adversarial/` (needs the two conftest.py sys.path fixes already in place — no changes needed, just needs to run from repo root).
  - Full ~91+ test count, currently only ever run manually.
- [ ] **`frontend.yml`** — `npm ci`, `tsc -b`, `vite build`, and (once Phase 1's 1.5 lands) `npm run test` (Vitest) + the Playwright golden-path test.
- [ ] **`docker-build.yml`** — builds all 6 Docker images (5 backend + frontend) as a smoke test on every push to the main branch — catches a broken Dockerfile/requirements.txt before it's discovered by someone trying to run `docker compose up`, not pushed anywhere yet at this stage (image push/registry wiring is Stage B's `deploy.yml`, not this one).
- [ ] Branch protection: require `test.yml` and `frontend.yml` to pass before merging to the main branch.

**Stage A acceptance criteria:** a person who has never touched this project can clone it, run one documented command, and have the full system (including the real Kind cluster canary demo) working, with no step requiring them to already understand the internals.

## Stage B — Global deployment (only after Stage A is genuinely solid)

### B.1 — Where the *control plane* runs
Two realistic options, evaluated against the earlier research (Flagger/Argo Rollouts run as in-cluster controllers; a cheap VPS is the pragmatic default for a small team):

- **Option 1 — Single VPS + docker-compose + reverse proxy** (recommended starting point): the exact stack that already works locally, deployed to one cloud VM (DigitalOcean/Hetzner/similar, ~$12–24/mo for enough RAM to run the full stack), fronted by Caddy for automatic TLS. Lowest operational complexity, matches the earlier research's conclusion that Kubernetes is unnecessary complexity for hosting a docker-compose-shaped app on one box.
- **Option 2 — The control plane itself runs on Kubernetes** (via a Helm chart): more natural if the *target users* already standardize on Kubernetes for their own infra and expect to install this the same way they install Flagger/Argo Rollouts (as an in-cluster controller). Meaningfully more work: needs a Helm chart, an ingress controller, persistent volume claims for Postgres, and someone comfortable operating Kubernetes for the platform's own sake, not just as the thing it manages.

**Recommendation: start with Option 1.** It reuses everything already built and tested (the exact `docker-compose.yml` already proven working), and defer Option 2 until there's a concrete reason (a customer who specifically wants an in-cluster install, or a genuine need for the platform itself to scale beyond one VM).

### B.2 — What "global" actually requires, once Option 1 is running for one team
- [ ] **Multiple environments** — dev/staging/prod separation (currently one flat `.env`), so changes can be validated before touching whatever's serving real teams.
- [ ] **Database backups** — automated Postgres backups (the audit ledger and verification history are the kind of data a team would be upset to lose) with a tested restore procedure, not just "backups exist somewhere."
- [ ] **Multi-region, if actually needed** — only relevant once there are real users in geographically distant regions experiencing real latency to a single-region deployment; this is not a "build it because it's more scalable" item, it's a "build it when a real user complains" item. Premature multi-region adds real operational cost (data replication/consistency questions across the RLS-protected tenant data) for a problem that may never materialize.
- [ ] **A real domain + DNS** with the TLS setup from B.1, and CORS/`CORS_ALLOW_ORIGINS` config updated from the current `localhost:3000` default.
- [ ] **`deploy.yml` (GitHub Actions)** — a new workflow, not an extension of `test.yml`: on merge to the main branch, gated on `test.yml`/`frontend.yml`/`docker-build.yml` passing, build + push images to a registry (GitHub Container Registry — free, same platform, no separate account) then SSH/deploy to the VPS from B.1 (`docker compose pull && docker compose up -d`, or a small deploy script). Shipping a change to the live platform becomes a merge, not a manual SSH session.

## Explicit non-goals for now
- Multi-region active-active is not a target until Stage B has been running successfully for real users in one region — don't build for a scale problem that doesn't exist yet.
- A managed Kubernetes offering (EKS/GKE/AKS) for the control plane specifically is not recommended unless Option 2 (B.1) is chosen for a concrete reason — it's meaningfully more expensive and operationally heavier than a VPS for the same workload at this scale.

## Acceptance criteria (Stage B, when reached)
- The platform is reachable at a real domain over HTTPS, running the exact same code validated in Stage A.
- A deploy (new image, migrated schema) happens via CI/CD on merge to a designated branch, not a manual SSH + `docker compose up` session.
- A restore-from-backup has actually been tested once, not just configured.

## Depends on
Every prior phase — this is explicitly the last one, and Stage B specifically should not start until Stage A's local hardening is genuinely complete.
