# Phase 4 — Security Hardening

**Status: mostly done, verified live end-to-end. Transport security (4.3) and full secrets-store migration (4.4) are deliberately deferred to Phase 7 — see the scope note below.**

## Goal
The platform is safe to expose to real, mutually-untrusting teams and to the open internet, not just to a trusted local user.

## Why now
Phase 3 introduces per-service actions worth protecting (deploying real infrastructure on someone's behalf) — this is the point where "who is allowed to do what" stops being a theoretical concern.

## What shipped

### 4.1 — Authorization (not just authentication)
- [x] `services/api-gateway/src/auth/rbac.py` — `require_role(min_role)` FastAPI dependency, ordinal role hierarchy `developer(1) < lead-sre(2) < platform-admin(3)`.
- [x] `auth/middleware.py` now extracts and verifies the JWT's `role` claim onto `request.state.role` (previously only `tenant_id` was extracted — `role` was signed and verified but never read anywhere).
- [x] Applied to: `pause`/`resume`/`rollback` (`actuation_router.py`), `save_policy` (`policy_router.py`), `register_service` (`services_router.py`, Phase 3's onboarding endpoint) — all require `lead-sre`+. Triggering an ordinary rollout and reading pipeline/verification/audit data stay open to any authenticated `developer` — this platform's actual day-to-day workflow.
- [x] Live-verified: `demo@other-corp.test` (`developer`) gets `403` on pause/register-service; `demo@acme-corp.test` (`platform-admin`) passes the gate (gets `404` on a made-up run id — proof the handler actually ran).

### 4.2 — Auth hardening
- [x] **Login rate limiting**: Redis-backed counter (`login_attempts:{email}`), 5 failures locks out for 300s (`429`, not `401`) — the lockout window anchors to the *first* failure (`incr` + `expire` only on the first increment), not reset by each subsequent attempt, so a slow-drip attacker can't evade it by pacing requests.
- [x] **Refresh tokens**: access tokens cut from 8 hours to 15 minutes; a new opaque refresh token (`secrets.token_urlsafe(32)`, 7-day TTL) is stored in Redis as an allow-list (`refresh_token:{token}` → claims) — the allow-list doubles as the revocation mechanism a stateless JWT alone can't give you. Rotated on every use (old key deleted, new one issued) and revoked immediately on logout.
- [x] **Structured auth audit trail**: new `auth_events` table (migration `0004`, plus `schema.sql` for fresh installs) — `LOGIN_SUCCESS`/`LOGIN_FAILURE`/`LOGIN_LOCKED_OUT`/`TOKEN_REFRESH`/`TOKEN_REFRESH_REJECTED`/`LOGOUT`, queryable, not stdout-only.
- [x] **Frontend wired to the new flow** (`frontend/src/api/client.ts`, `auth.ts`, `lib/auth-store.ts`): a `401` triggers exactly one silent refresh-and-retry (deduped across concurrent in-flight requests via a shared promise) before forcing a real re-login; logout now calls the server to revoke the refresh token, not just a local `clearSession()`.

### 4.3 — Transport security — **deferred to Phase 7**
Caddy/TLS termination and the mTLS decision are genuinely deployment-target-dependent (a reverse proxy in front of `localhost` has no real certificate to terminate, and Let's Encrypt needs a public domain) — exactly the kind of decision the user already asked to defer once (GitHub Actions/AWS work was explicitly pushed to "later, after the main product"). Building it now against `localhost` would be theater, not hardening. Revisit when Phase 7 picks a concrete deployment target.

### 4.4 — Secrets management — **partially shipped, rest deferred to Phase 7**
- [x] **`VERDICT_SIGNING_KEY` rotation support** — the one concrete secrets-management sub-item that's pure code, testable locally, and deployment-target-independent. `policy-controller/src/verdict_verifier.py` now accepts an optional `VERDICT_SIGNING_KEY_PREVIOUS` env var: during a rotation, a verdict signed a moment ago with the outgoing key still verifies (logged as `verdict_verified_with_previous_signing_key` so an operator knows when it's safe to drop the old key), while the default (no previous key configured) behavior is unchanged — a verdict signed with any key but the current one is still rejected. Covered by 2 new unit tests in `services/policy-controller/tests/test_verdict_verifier.py`.
- [ ] Migrating off a plaintext `.env` to Docker secrets / a cloud secrets manager / Kubernetes `Secret` objects depends on which Phase 7 deployment target gets picked — deferred alongside 4.3 for the same reason.

### 4.5 — Network-level reinforcement of the structural boundary
- [x] Investigated live rather than assumed: a real `NetworkPolicy` doesn't apply here — verification-engine only ever runs as a `docker-compose` service, never as a pod in the Kind cluster, so there's no cluster-scoped object to attach one to.
- [x] **A real, non-obvious finding**: verification-engine (only on the compose `default` network, never `kind`) can still open a raw TCP connection to the Kind control-plane's API server address — Docker Desktop's bridge-network isolation is weaker than native Linux dockerd's, which by default `iptables`-blocks cross-bridge traffic. Raw reachability turned out not to matter: an actual API call authenticates as `system:anonymous` and is rejected `403` by Kubernetes' own RBAC, because verification-engine has no `kubernetes` package and no kubeconfig. But this means the boundary is currently held entirely at the code level, not the network level — worth knowing, and worth guarding against regressing.
- [x] `tests/adversarial/test_verification_engine_k8s_boundary.py` — 3 new tests, AST-parsing verification-engine's actual source (not grepping) to assert it never imports a `kubernetes` client, never declares one in `requirements.txt`, and never references a kubeconfig/in-cluster-credential discovery path. Runs standalone, no live infra needed.
- [x] `scripts/setup/verify_network_boundary.sh` (+ `make verify-network-boundary`) — the live version of the same check against the real running stack and real Kind cluster: no `kubernetes` pip package, no kubeconfig, and a real HTTPS call to the real API server address gets a real `403`.

## Honest scope note: 4.3/4.4 deferred, not skipped
TLS termination and full secrets-store migration are real Phase 4 deliverables in the original plan, but both are meaningless to build against `localhost` and genuinely depend on Phase 7's deployment target (a single VM gets Caddy + Docker secrets; a Kubernetes control plane gets Traefik + sealed-secrets/external-secrets; a managed cloud target might use its own secrets manager entirely). Building either now would mean redoing it once Phase 7 picks a target. This mirrors the user's own earlier explicit instruction to defer deployment-specific work (GitHub Actions, AWS) until "the main product" is done — 4.3/4.4 are exactly that kind of work wearing a Phase 4 label.

## Acceptance criteria
- [x] A `developer`-role user gets a `403`, not a `200`, when attempting to pause a pipeline or register a new service.
- [x] Repeated rapid failed login attempts against the same account result in a temporary lockout (`429`), verified by an adversarial test.
- [x] All secrets required to run the stack are absent from any file that would be committed to git (`.env` is gitignored; `.env.example` carries only placeholder/dev-only defaults, already true before this phase).
- [x] Existing test suites still pass unmodified, plus new coverage: 68 (verification-engine) + 5 (policy-controller, incl. 2 new rotation tests) + 3 (pipeline-worker) + 7 (explainability-service) + 24 (adversarial, incl. 3 new network-boundary + 8 new auth-hardening-live) + 4 (OPA) + 12 (frontend unit, incl. 3 new refresh-flow tests) + 7 (frontend e2e) — all green.

## Depends on
Phase 3 (Multi-Service Onboarding) — done; role/permission enforcement was designed against the real shape of "who can do what to which service" that Phase 3 defined (onboarding a service is now explicitly gated the same way pause/rollback are).
