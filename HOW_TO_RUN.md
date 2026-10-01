# How to Run This Project — Step by Step

One self-contained walkthrough: clone → configure → start the backend → start the frontend → log in →
(optionally) run tests → shut down. For the full exhaustive reference on every env var and a
troubleshooting deep-dive, see `SETUP.md`, `README.md`, and `KNOWLEDGE_BASE.md` — this file is the
quick, linear path to get from nothing to a fully working, logged-in instance.

---

## 1. Prerequisites

Install these first:

| Tool | Why | Check it's installed |
|---|---|---|
| **Docker Desktop** (with Compose v2) | runs the whole backend stack | `docker compose version` |
| **Node.js 20.x** | runs the frontend dev server | `node -v` |
| **Git** | clone the repo | `git --version` |
| **Python 3.11 or 3.12** (optional) | only needed to run backend tests or generate secrets locally | `python --version` |

Optional, only if you want the real Kubernetes/Envoy-Gateway path (§8 below) or backend tests from the
command line:
- `kubectl`, `kind` — real local Kubernetes target
- OPA CLI — on Windows, `bin/opa.exe` is already vendored in the repo, no separate install needed

**Windows note**: this repo's folder name contains an `&` (`Smart AI DevOps & Continuous Delivery
Platform`), which breaks npm's `.cmd` shims under a cmd.exe-backed shell — `npm run dev` /
`npm run build` can silently fail or truncate. Everywhere below that matters, this guide calls the
underlying script directly instead (`node node_modules/vite/bin/vite.js`, etc.) so you don't hit it.
Plain `docker`, `git`, `kubectl`, `kind`, `python` are unaffected.

---

## 2. Clone the repository

```bash
git clone https://github.com/tusharharbinger-collab/Smart-AI-DevOps-Continuous-Delivery-Platform.git
cd Smart-AI-DevOps-Continuous-Delivery-Platform
```

---

## 3. Configure your environment

`.env` is gitignored on purpose (it holds real secrets) — copy the template and fill it in:

```bash
cp .env.example .env
```

Open `.env` and set these. Everything else in the file already has a safe local default and can be
left as-is for a first run.

**Required — generate fresh, don't reuse values from another machine:**

```bash
# Run this twice — once for each key below
python -c "import secrets; print(secrets.token_hex(32))"
```

- `VERDICT_SIGNING_KEY` — paste one generated value here (shared only between `verification-engine`
  and `policy-controller`, signs every canary verdict).
- `JWT_SECRET_KEY` — paste the other generated value here (signs login session tokens).

**Optional — only needed for specific features:**

- `GROQ_API_KEY` — get a free key at https://console.groq.com/keys. Without it, the AI features
  (root-cause explanations, the Infra Architect Agent, ChatOps, log-hygiene scanning) still work, just
  via a deterministic fallback instead of real LLM text.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — only needed for the "Connect GitHub" button in the
  New Service wizard. Register a free OAuth App at GitHub → Settings → Developer settings → OAuth Apps,
  with Homepage URL `http://localhost:3000` and Authorization callback URL
  `http://localhost:8000/api/v1/integrations/github/callback` (must match exactly). See `SETUP.md` §3
  for the full click-by-click version.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` — only needed if you want to deploy a
  real project to AWS ECS Fargate (§9 below). Without these, everything else still runs — pipelines,
  statistical verification, the UI — just with no real cloud target to deploy *to*.

---

## 4. Start the backend

```bash
docker compose up -d --build
```

First run builds every image, so it takes a few minutes. This brings up 15 containers: Postgres,
Redis, OPA, MinIO, Prometheus, Loki, Promtail, Grafana, the 5 backend services
(`api-gateway:8000`, `pipeline-worker:8001`, `verification-engine:8002`, `policy-controller:8003`,
`explainability-service:8004`), and a sample-app baseline/canary pair + load generator.

Check everything came up healthy:

```bash
docker compose ps
curl localhost:8000/healthz
curl localhost:8000/readyz
```

`readyz` should report `{"ready":true,"checks":{"database":"ok","redis":"ok","opa":"ok"}}`. If any
container isn't healthy, check its logs: `docker compose logs <service-name> --tail 50`.

Full interactive API docs (Swagger) are live at **`localhost:8000/docs`**.

Grafana is at **`localhost:3001`** (`admin`/`admin`) — Prometheus and Loki are already wired as
datasources with a "Platform Health" dashboard pre-loaded.

---

## 5. Create a login account

There's no signup page — login only checks existing rows in the database. On a brand-new database,
create the demo account once:

```bash
docker compose exec postgres psql -U platform -d platform -c "
INSERT INTO tenants (name) VALUES ('acme-corp') ON CONFLICT DO NOTHING;
"
```

Generate a bcrypt hash for the password (needs `pip install bcrypt` once if you don't have it):

```bash
python3 -c "
import bcrypt
print(bcrypt.hashpw('acme-demo-2026'.encode(), bcrypt.gensalt()).decode())
"
```

Insert the user, substituting the hash printed above:

```bash
docker compose exec postgres psql -U platform -d platform -c "
INSERT INTO users (tenant_id, email, password_hash, role)
SELECT tenant_id, 'demo@acme-corp.test', '<paste-the-hash-here>', 'platform-admin'
FROM tenants WHERE name = 'acme-corp'
ON CONFLICT (email) DO NOTHING;
"
```

You can log in afterward with `demo@acme-corp.test` / `acme-demo-2026`. (If you're restoring an
existing Postgres volume that already has this account, skip this whole step.)

---

## 6. Start the frontend

The frontend is **not** part of `docker-compose.yml` by design — run it locally, pointed at the
dockerized backend:

```bash
cd frontend
npm install
node node_modules/vite/bin/vite.js
```

(Not `npm run dev` — see the Windows note in §1.) Leave this running in its own terminal; it serves
the dev server at **`http://localhost:3000`**.

If `VITE_API_BASE_URL` isn't already set, create `frontend/.env` with:

```
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws
```

---

## 7. Log in and use it

Open **`http://localhost:3000`**, log in with the account from step 5.

You land on **Projects Overview** (`/projects`) — a card per onboarded service.

- **New Service** walks through the 3-step wizard: connect a GitHub repo (or an existing container
  image), configure build/test, set networking and the canary rollout policy.
- An existing project card opens its **workspace** (`/projects/:id`) with four tabs: **Pipeline View**
  (live stage execution + the infra/pipeline graph), **Verification Inspector** (the statistical
  verdict and evidence), **Policy & Gates** (editable YAML guardrails), **Audit Ledger** (every
  autonomous decision, exportable as CSV).
- **Trigger New Rollout** in a project's workspace starts a real pipeline run.

---

## 8. Running the test suites (optional)

```bash
# Per-service backend tests (run from inside each service's directory)
cd services/verification-engine && python -m pytest tests/ -v
cd services/policy-controller && python -m pytest tests/ -v
cd services/pipeline-worker && python -m pytest tests/ -v
cd services/explainability-service && python -m pytest tests/ -v
cd services/api-gateway && python -m pytest tests/ -v

# OPA policy tests (needs the stack up for some; bin/opa.exe is vendored on Windows)
opa test policies/ -v

# Cross-service adversarial tests (repo root; stack must be up)
python -m pytest tests/adversarial/ -v

# Frontend unit tests (from frontend/)
node node_modules/vitest/vitest.mjs run

# Frontend type-check + build
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build

# Frontend end-to-end (Playwright; stack + frontend must both be up)
node node_modules/playwright/cli.js test
```

All of the above should pass green against a correctly set-up stack.

---

## 9. Real AWS ECS deployment target (optional)

If you set the `AWS_*` credentials in step 3, you can deploy a real project to AWS Fargate behind a
shared Application Load Balancer — this is the platform's primary real deploy target. No extra setup
commands are needed beyond the credentials; the first project you onboard with `deploy_target:
"aws_ecs"` automatically creates the shared ALB and ECS cluster for you (takes 2–3 minutes the very
first time). This is real, billable AWS infrastructure — remember to delete a project from the UI
when you're done with it so nothing keeps running.

---

## 10. Real Kubernetes target (optional, alternative to §9)

```bash
make kind-up               # creates a 3-node local Kind cluster
make install-envoy         # installs Envoy Gateway + Gateway API CRDs
make deploy-sample-app     # builds/loads sample-app images, applies gateway + canary manifests
make connect-kind-network  # wires pipeline-worker/policy-controller to the live cluster
docker compose up -d pipeline-worker policy-controller   # recreate so they pick up the new kubeconfig
```

On Windows, `make` and `kind` are not normally on PATH — call `./bin/kind.exe` directly and run the
`Makefile`'s recipe bodies by hand if `make` itself isn't available.

Verify with `curl localhost:8001/readyz` — it should report `"kubernetes": "ok"`.

---

## 11. Shutting everything down

```bash
# Stop the frontend dev server: Ctrl+C in its terminal

# Stop the backend containers (keeps your data — Postgres/Redis volumes persist)
docker compose down

# Only if you also stood up the real Kind cluster:
make kind-down
```

To start again later, just repeat steps 4 and 6 — your database, demo account, and any projects you
created are still there (container images don't need rebuilding unless the code changed, so a plain
`docker compose up -d` without `--build` is enough on a second run).

---

## Quick troubleshooting

| Symptom | Likely cause |
|---|---|
| A container isn't healthy | `docker compose logs <service> --tail 50` — usually a missing/invalid env var |
| Login fails for the demo account | Step 5 wasn't run against this Postgres volume yet |
| Frontend can't reach the backend | Check `frontend/.env`'s `VITE_API_BASE_URL` points at `:8000` |
| `npm run dev` silently fails/truncates | Windows `&`-in-folder-name trap — use `node node_modules/vite/bin/vite.js` instead (§1) |
| Verdicts suddenly rejected as forged | `VERDICT_SIGNING_KEY` drifted across containers — `docker compose up -d verification-engine policy-controller` (not `restart`) after any `.env` change to that key |
| `alembic upgrade head` fails on an old database | See `SETUP.md` §5 — migrations 0001–0009 are idempotent, so applying one's SQL directly via `psql` is a safe fallback |

For anything not covered here: `SYSTEM_GUIDE.md` (complete current reference), `KNOWLEDGE_BASE.md`
(diagnostic trail of every real bug hit and fixed), and `SETUP.md` (the exhaustive from-scratch
version of this same walkthrough).
