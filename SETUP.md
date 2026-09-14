# SETUP.md — Cloning this project onto a new machine

This is the step-by-step to go from `git clone` to an identical, fully-working
copy of this platform on a different laptop — same logins, same GitHub OAuth
app, same behavior. `README.md` has the short quick-start; this file exists so
nothing has to be re-discovered or re-guessed the second time around.

Everything here reflects the real, currently-working setup on the original
machine as of 2026-09-14 (commit `f2e634e`).

---

## 1. Prerequisites

Install these before anything else:

| Tool | Version used | Check |
|---|---|---|
| Docker Desktop (with Compose v2) | any recent | `docker compose version` |
| kubectl | any recent | `kubectl version --client` |
| kind | any recent | `kind version` |
| Node.js | 20.x | `node -v` |
| Python | 3.11 or 3.12 | `python --version` |
| git | any recent | `git --version` |
| OPA (Open Policy Agent) CLI | any recent | `opa version` |

On Windows, `bin/opa.exe` is already vendored in the repo — you don't need to
install OPA separately just to run `opa test`, but Docker Desktop, kubectl,
kind, Node, and Python are still real installs.

**Windows Git Bash gotcha**: this repo's folder name contains an `&`
character (`Smart AI DevOps & Continuous Delivery Platform`). cmd.exe treats
`&` as a command separator, which breaks npm's `.cmd` shims — `npm run dev`,
`npm run build`, etc. from a shell that shells out through cmd.exe will
silently fail or truncate the command. If you hit that, call the underlying
script directly instead:

```bash
node node_modules/vite/bin/vite.js            # instead of `npm run dev`
node node_modules/.bin/tsc -b                 # instead of `npm run build`'s type-check step
```

Plain `docker`, `git`, `kubectl`, `kind`, and `python` invocations are
unaffected — only npm's shim wrapper is.

---

## 2. Clone

```bash
git clone <your-remote-url> "Smart AI DevOps & Continuous Delivery Platform"
cd "Smart AI DevOps & Continuous Delivery Platform"
```

---

## 3. Configure `.env`

`.env` is gitignored on purpose (it holds real secrets) — it does **not**
travel with the clone. Copy the template and fill it in:

```bash
cp .env.example .env
```

Then edit `.env`. Here is what every variable is for and exactly how to get
a real value for it:

### Required to boot at all
These already have safe local defaults in `.env.example` — you generally
don't need to touch them for a local Docker Compose setup:
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_DSN`
- `REDIS_URL`
- `OPA_URL`
- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` / `AWS_ENDPOINT_URL`
- `LOG_LEVEL`, `CPU_COST_PER_VCPU_HOUR`, `MEM_COST_PER_GIB_HOUR`

### Required for real functionality — generate these fresh per machine

**`VERDICT_SIGNING_KEY`** — HMAC-256 secret shared only between
`verification-engine` and `policy-controller` to sign/verify verdicts.
Generate a new one (don't reuse the old machine's — it's not meant to be
shared outside the pair of containers that use it):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**`JWT_SECRET_KEY`** — signs session access tokens. Generate the same way:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

> **Important**: if you ever change either of these keys while the stack is
> already running, you must `docker compose up -d <service>` (not
> `restart`) for the affected containers — Compose only re-reads `.env` at
> container *creation* time, not on restart. A stale signing key on just one
> container causes every verdict to be silently rejected as forged with no
> other symptom.

### Optional — real LLM-generated RCA text

**`GROQ_API_KEY`** — get a free key at https://console.groq.com/keys. Without
it, `explainability-service` still works, but falls back to a deterministic
(non-LLM) report template instead of live Groq-generated prose.
`GROQ_MODEL` defaults to `openai/gpt-oss-120b` — leave as-is unless you want
a different Groq-hosted model.

### Optional — Slack alerting

**`SLACK_WEBHOOK_URL`** — leave blank to have alerts log to stdout instead
(this is fine for local dev/demo).

### Optional but needed for the "Connect GitHub" wizard flow

**GitHub OAuth App** — needed only if you want the New Service wizard's
"Connect GitHub" repo picker to work. Register one per machine/environment
(the redirect URI is tied to `localhost`, so a laptop-specific app is
correct, not a mistake):

1. GitHub → Settings → Developer settings → OAuth Apps → **New OAuth App**.
2. Fill in exactly:
   - **Application name**: anything (e.g. `AI DevOps - <your machine>`)
   - **Homepage URL**: `http://localhost:3000`
   - **Authorization callback URL**: `http://localhost:8000/api/v1/integrations/github/callback`
   - The callback URL must match `GITHUB_OAUTH_REDIRECT_URI` below **exactly**
     — a mismatch fails with `redirect_uri_mismatch`.
3. Register it, then click **Generate a new client secret**.
4. Put both into `.env`:
   ```
   GITHUB_CLIENT_ID=<the Client ID shown on the app page>
   GITHUB_CLIENT_SECRET=<the secret you just generated — shown only once>
   GITHUB_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/integrations/github/callback
   FRONTEND_BASE_URL=http://localhost:3000
   ```
5. The OAuth token itself is never stored in Postgres — it lives in Redis
   keyed by user id, so it doesn't need to be backed up or migrated.

**`GITHUB_TOKEN`** (optional) — a classic personal access token used as a
fallback by `pipeline-worker` only when a user hasn't connected their own
GitHub account via OAuth, for cloning private repos server-side. Leave blank
if you'll only build from public repos or from an existing image.

---

## 4. Bring up the stack

```bash
docker compose up -d --build
```

This builds and starts all 16 containers: Postgres, Redis, OPA, MinIO,
Prometheus, Loki, Promtail, Grafana, the 5 backend services
(`api-gateway:8000`, `pipeline-worker:8001`, `verification-engine:8002`,
`policy-controller:8003`, `explainability-service:8004`), the frontend
(`localhost:3000`), and the sample-app baseline/canary pair + load
generator.

Check health:

```bash
docker compose ps
curl localhost:8000/healthz && curl localhost:8000/readyz
```

Or use the Makefile shortcut: `make healthcheck`.

Grafana (`localhost:3001`, `admin`/`admin`) comes pre-wired with Prometheus
and Loki datasources and a "Platform Health" dashboard — no extra setup.

---

## 5. Database migrations

The Docker images run migrations as part of startup for a genuinely fresh
database, so step 4 alone is usually enough. If you ever need to run them
by hand:

```bash
make migrate     # = cd services/api-gateway && python -m alembic upgrade head
```

**Known trap**: on a long-lived dev database, `alembic upgrade head` can
fail because `alembic_version` bookkeeping has drifted (this has happened
on the original machine from earlier manual intervention). Migrations
`0001`–`0009` are all idempotent (`CREATE TABLE IF NOT EXISTS`,
`ADD COLUMN IF NOT EXISTS`, etc.), so if `alembic upgrade head` errors out,
applying the specific migration's SQL directly is a safe fallback:

```bash
docker compose exec -T postgres psql -U platform -d platform < migrations/versions/000X_something.sql
```

(or paste the SQL body of the migration directly into
`docker compose exec postgres psql -U platform -d platform`). On a **fresh**
clone with a brand-new Postgres volume, you will not hit this — it only
happens after a database has accumulated real history across multiple
manual interventions.

---

## 6. Demo accounts

There is **no signup endpoint** and **no automatic user seed** — login only
checks existing rows in the `users`/`tenants` tables. On the original
machine, two demo accounts already exist in the Postgres volume. A brand
new database will not have them, so create them once:

```bash
docker compose exec postgres psql -U platform -d platform -c "
INSERT INTO tenants (name) VALUES ('acme-corp'), ('other-corp')
ON CONFLICT DO NOTHING;
"
```

Then generate bcrypt hashes for the two passwords and insert the users
(run this Python snippet locally — it just needs the `bcrypt` package,
`pip install bcrypt` if you don't have it):

```bash
python3 -c "
import bcrypt
for pw in ['acme-demo-2026', 'other-demo-2026']:
    print(pw, '->', bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode())
"
```

Then insert, substituting the two hashes printed above:

```sql
-- run inside: docker compose exec postgres psql -U platform -d platform
INSERT INTO users (tenant_id, email, password_hash, role)
SELECT tenant_id, 'demo@acme-corp.test', '<hash-for-acme-demo-2026>', 'platform-admin'
FROM tenants WHERE name = 'acme-corp'
ON CONFLICT (email) DO NOTHING;

INSERT INTO users (tenant_id, email, password_hash, role)
SELECT tenant_id, 'demo@other-corp.test', '<hash-for-other-demo-2026>', 'developer'
FROM tenants WHERE name = 'other-corp'
ON CONFLICT (email) DO NOTHING;
```

Result: `demo@acme-corp.test` / `acme-demo-2026` (platform-admin, tenant
acme-corp — this is the account with real pipeline history) and
`demo@other-corp.test` / `other-demo-2026` (developer, tenant other-corp —
deliberately empty, used to prove tenant isolation).

If you'd rather not hand-run SQL, `make seed` inserts the `acme-corp` tenant
row only — you'd still need the `users` insert above.

---

## 7. Real Kubernetes target (optional but recommended)

Without this, pipelines still run and produce statistically real verdicts,
but nothing actually shifts traffic on a cluster. To wire up the real Kind +
Envoy Gateway path:

```bash
make kind-up                # creates a 3-node Kind cluster (k8s/kind-config.yaml)
make install-envoy          # installs Envoy Gateway + Gateway API CRDs
make deploy-sample-app      # builds/loads sample-app images, applies gateway + canary manifests
make connect-kind-network   # wires pipeline-worker/policy-controller to the live cluster
docker compose up -d pipeline-worker policy-controller    # recreate so they pick up the new kubeconfig
```

Verify: `curl localhost:8001/readyz` should report `"kubernetes": "ok"`.

From here, a `HEALTHY` verdict genuinely shifts real `HTTPRoute` traffic
(watch with `kubectl get httproute payment-service-route -n production -w
--context kind-smartcd-local`), and a `FAILED` verdict genuinely scales the
canary `Deployment` to 0 and rolls back — no manual steps.

---

## 8. Using the UI

Open `localhost:3000`, log in with a demo account from step 6. Landing page
is **Projects Overview** (`/projects`). **New Service** walks through the
3-step wizard (GitHub repo or existing image → build/test/networking config
→ canary policy). An existing project card opens its workspace
(`/projects/:id`) with four tabs: Pipeline View, Verification Inspector,
Policy & Gates, Audit Ledger.

**Naming rule enforced by the platform**: project names get slugified into
lowercase Kubernetes-safe identifiers automatically (Kubernetes and Docker
both reject uppercase in resource/image names) — you can type a
mixed-case project name freely, but a manually-typed `container_image` must
already be lowercase or project creation is rejected with a 422 at creation
time, not mid-build.

---

## 9. Verify the clone is working identically

```bash
# All services healthy
make healthcheck

# Per-service unit tests
cd services/verification-engine && python -m pytest tests/ -v && cd ../..
cd services/policy-controller && python -m pytest tests/ -v && cd ../..
cd services/pipeline-worker && python -m pytest tests/ -v && cd ../..
cd services/explainability-service && python -m pytest tests/ -v && cd ../..

# OPA policy tests
opa test policies/ -v

# Cross-service adversarial + live integration tests (stack must be up)
python -m pytest tests/adversarial/ -v

# Frontend e2e (stack must be up)
cd frontend && node node_modules/.bin/playwright test    # or: npm run test:e2e
```

All of the above should be green. If `tests/adversarial/test_*_live.py`
tests skip instead of running, it means `api-gateway` isn't reachable on
`localhost:8000` — check `docker compose ps` first.

---

## 10. What does NOT travel with the clone (by design)

- `.env` — gitignored, recreate per step 3.
- The GitHub OAuth app's client secret — register a fresh app per step 3;
  it's tied to `localhost` anyway, so a shared app across machines doesn't
  make sense.
- Postgres/Redis/MinIO data volumes — a fresh clone gets a fresh (empty)
  database; demo accounts and any project/pipeline history must be
  recreated (step 6) or built up again by using the app.
- `VERDICT_SIGNING_KEY` / `JWT_SECRET_KEY` — generate new ones; they only
  need to be consistent *within* one machine's set of containers, never
  shared across machines.
