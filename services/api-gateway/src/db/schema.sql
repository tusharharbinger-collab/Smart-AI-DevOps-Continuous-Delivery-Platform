-- services/api-gateway/src/db/schema.sql
-- ─────────────────────────────────────────────────────────────────
-- Complete PostgreSQL 16 schema for the Smart AI DevOps & CD Platform.
-- Every tenant-scoped table has Row-Level Security (RLS) enabled AND
-- forced — including for the table owner — so no application-level
-- bug can bypass tenant isolation.
--
-- Applied via Alembic migration (see migrations/versions/0001_initial.py).
-- Can also be applied directly: psql -U platform -d platform -f schema.sql
-- ─────────────────────────────────────────────────────────────────

-- Extension for server-side UUID generation (gen_random_uuid())
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─────────────────────────────────────────────────────────────────
-- TENANTS  (no RLS — this is the root anchor table; access is
--            controlled at the application/auth layer)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────
-- USERS  (no RLS — same reasoning as `tenants`: logging in happens
--          before a tenant context exists, so the login lookup must be
--          able to find a user by email across all tenants. Nothing
--          sensitive beyond a bcrypt hash lives here.)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    email         TEXT        NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    role          TEXT        NOT NULL DEFAULT 'developer'
                      CHECK (role IN ('developer', 'lead-sre', 'platform-admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────
-- PIPELINES
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    policy_yaml  TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL until this pipeline's canary_deploy stage has genuinely
    -- succeeded once. A project's very first-ever deployment has no prior
    -- version to compare against, so it skips canary_loop's statistical
    -- verification and ships straight to 100% on both baseline and
    -- canary (matches Argo Rollouts' documented first-deployment
    -- behavior) — see migration 0010.
    first_deployment_completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, name)
);

ALTER TABLE pipelines ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipelines FORCE ROW LEVEL SECURITY;   -- applies even to table owner

-- PERMISSIVE (the default — do not add `AS RESTRICTIVE` here). A RESTRICTIVE
-- policy only narrows an existing grant; with no PERMISSIVE policy on a
-- table, Postgres denies every row to everyone, which would make the app's
-- own tenant see nothing rather than just its own rows. RESTRICTIVE is for
-- an additional policy that further narrows this one (e.g. a read-only-mode
-- flag), not for the base tenant-isolation grant itself.
CREATE POLICY tenant_isolation_pipelines ON pipelines
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- PROJECTS  (Phase 8 — Render-style project workspaces)
--
-- A project OWNS a `pipelines` row: the project holds the repo/build/image
-- metadata a user configures in the creation wizard, its pipeline holds the
-- generated declarative YAML the worker actually executes. Deliberately a
-- wrapper rather than a replacement — verification_records, audit_ledger,
-- approvals and cost_analysis all FK to pipeline_executions, so a project
-- rollout has to BE an ordinary pipeline execution for those to keep
-- working. See docs/roadmap/08-project-workspaces.md.
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    project_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_id           UUID        REFERENCES pipelines(pipeline_id) ON DELETE SET NULL,
    name                  TEXT        NOT NULL,
    -- Nullable: a pipeline registered directly through the API (no wizard,
    -- no repo) is adopted into a project so it stays reachable in the UI.
    -- Those rows genuinely have no repository or image, and are rendered as
    -- "No repository connected" rather than given invented values.
    repo_url              TEXT,
    branch                TEXT        NOT NULL DEFAULT 'main',
    root_directory        TEXT        NOT NULL DEFAULT './',
    dockerfile_path       TEXT        NOT NULL DEFAULT 'Dockerfile',
    test_command          TEXT,
    container_image       TEXT,
    active_production_tag TEXT        NOT NULL DEFAULT 'v1.0.0',
    canary_tag            TEXT,
    status                TEXT        NOT NULL DEFAULT 'IDLE'
                              CHECK (status IN ('IDLE','BUILDING','TESTING','VERIFYING',
                                                'HEALTHY','ROLLED_BACK','FAILED')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_projects ON projects
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- PIPELINE EXECUTIONS
--
-- project_id/trigger_type/commit_* (Phase 8) are nullable on purpose: a
-- run triggered against a hand-registered pipeline (POST /api/v1/pipelines)
-- has no project and no git provenance, and must stay valid.
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_executions (
    pipeline_run_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_id           UUID        NOT NULL REFERENCES pipelines(pipeline_id),
    project_id            UUID        REFERENCES projects(project_id) ON DELETE CASCADE,
    trigger_type          TEXT,
    commit_sha            TEXT,
    commit_message        TEXT,
    target_version        TEXT        NOT NULL,
    status                TEXT        NOT NULL DEFAULT 'PENDING'
                              CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED',
                                                'PAUSED','AWAITING_APPROVAL','ROLLED_BACK')),
    current_stage         TEXT,
    current_traffic_weight INT        DEFAULT 0 CHECK (current_traffic_weight BETWEEN 0 AND 100),
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ
);

ALTER TABLE pipeline_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_executions FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_executions ON pipeline_executions
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- EXECUTION STATE  (Redis-mirrored durable state for crash recovery)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS execution_state (
    pipeline_run_id       UUID        PRIMARY KEY
                              REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
    tenant_id             UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    -- Phase 5 (reliability & scale): lets reconciler.py re-fetch the
    -- pipeline's registered manifest (pipelines.policy_yaml) to actually
    -- resume an interrupted run, not just know which run/stage it was on.
    pipeline_id           UUID        REFERENCES pipelines(pipeline_id),
    service_name          TEXT        NOT NULL,
    current_stage         TEXT        NOT NULL,
    current_traffic_weight INT        NOT NULL DEFAULT 0 CHECK (current_traffic_weight BETWEEN 0 AND 100),
    status                TEXT        NOT NULL
                              CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED',
                                                'PAUSED','AWAITING_APPROVAL','ROLLED_BACK')),
    last_updated          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE execution_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE execution_state FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_execstate ON execution_state
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- VERIFICATION RECORDS  (one row per verification verdict)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS verification_records (
    verdict_id        UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID          NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_run_id   UUID          NOT NULL REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
    status            TEXT          NOT NULL
                          CHECK (status IN ('HEALTHY','DEGRADED','FAILED','UNVERIFIABLE')),
    composite_score   NUMERIC(5,2)  NOT NULL CHECK (composite_score BETWEEN 0 AND 100),
    confidence        NUMERIC(4,3)  NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    evidence          JSONB         NOT NULL DEFAULT '{}',
    tier1_breaches    TEXT[]        NOT NULL DEFAULT '{}',
    rca_summary       TEXT,
    hmac_signature    TEXT          NOT NULL,
    timestamp_utc     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

ALTER TABLE verification_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE verification_records FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_verification ON verification_records
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- POLICY RULES  (versioned Rego snapshots per pipeline)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policy_rules (
    policy_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_id  UUID        NOT NULL REFERENCES pipelines(pipeline_id),
    rego_snapshot TEXT       NOT NULL,
    version      INT         NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE policy_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_rules FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_policy ON policy_rules
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- AUDIT LEDGER  (immutable actuation log — every Kubernetes action
--               must have a row here before it is executed)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_ledger (
    actuation_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_run_id  UUID        NOT NULL REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
    action           TEXT        NOT NULL
                         CHECK (action IN ('WEIGHT_UPDATE','ROLLBACK','PROMOTE',
                                           'SCALE_ZERO','APPROVE','BLOCK','RIGHTSIZING',
                                           'GRADUATE')),
    verdict          TEXT,
    confidence       NUMERIC(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    authorized_by    TEXT        NOT NULL,  -- e.g. "OPA:allow_action=true:rule=PROMOTE_STEP"
    policy_rule      TEXT,
    hmac_signature   TEXT        NOT NULL,  -- the verdict's HMAC at time of action
    canary_weight    INT         CHECK (canary_weight IS NULL OR canary_weight BETWEEN 0 AND 100),
    baseline_weight  INT         CHECK (baseline_weight IS NULL OR baseline_weight BETWEEN 0 AND 100),
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE audit_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_ledger FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_audit ON audit_ledger
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- APPROVALS  (manual gate sign-offs)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS approvals (
    approval_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_run_id  UUID        NOT NULL REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
    stage            TEXT        NOT NULL,
    approver_user_id UUID        NOT NULL,
    approver_role    TEXT        NOT NULL CHECK (approver_role IN ('lead-sre','platform-admin','developer')),
    approved_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_approvals ON approvals
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- COST ANALYSIS  (per-run resource cost delta records)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cost_analysis (
    cost_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    pipeline_run_id  UUID        NOT NULL REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
    baseline_cost    NUMERIC(10,4) NOT NULL,
    canary_cost      NUMERIC(10,4) NOT NULL,
    delta_percent    NUMERIC(6,2)  NOT NULL,
    rightsizing_rec  JSONB,        -- recommendation from cost_analyzer.py
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE cost_analysis ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_analysis FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_cost ON cost_analysis
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- STAGE LOGS  (Phase 8 — durable per-stage build/test/verify output)
--
-- Redis `logs:{run_id}` stays the live fast path (24h TTL, replayable
-- within a session); this is the durable record that survives it, mirroring
-- the Redis+Postgres split execution_state already uses.
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stage_logs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID        NOT NULL REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
    tenant_id   UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    stage_name  TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE stage_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE stage_logs FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_stage_logs ON stage_logs
    FOR ALL
    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid);

-- ─────────────────────────────────────────────────────────────────
-- AUTH EVENTS  (Phase 4 security hardening — queryable auth audit trail;
--               no RLS, same reasoning as `users`: a failed login with an
--               unknown/bad email has no tenant_id to scope by yet)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth_events (
    event_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES users(user_id) ON DELETE SET NULL,
    email       TEXT        NOT NULL,
    event_type  TEXT        NOT NULL
                    CHECK (event_type IN (
                        'LOGIN_SUCCESS', 'LOGIN_FAILURE', 'LOGIN_LOCKED_OUT',
                        'TOKEN_REFRESH', 'TOKEN_REFRESH_REJECTED', 'LOGOUT'
                    )),
    ip_address  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_events_email_created
    ON auth_events (email, created_at DESC);

-- ─────────────────────────────────────────────────────────────────
-- INDEXES  (every frequent query path is covered)
-- ─────────────────────────────────────────────────────────────────
-- Pipeline executions: list by tenant + recency
CREATE INDEX IF NOT EXISTS idx_executions_tenant_started
    ON pipeline_executions (tenant_id, started_at DESC);

-- Execution state: find RUNNING pipelines older than 2 minutes (for reconciler)
CREATE INDEX IF NOT EXISTS idx_execstate_status_updated
    ON execution_state (status, last_updated)
    WHERE status = 'RUNNING';

-- Verification records: list by tenant + recency
CREATE INDEX IF NOT EXISTS idx_verification_tenant_ts
    ON verification_records (tenant_id, timestamp_utc DESC);

-- Projects: list by tenant + recency (the /projects overview grid)
CREATE INDEX IF NOT EXISTS idx_projects_tenant_created
    ON projects (tenant_id, created_at DESC);

-- Pipeline executions: a single project's run history
CREATE INDEX IF NOT EXISTS idx_executions_project_started
    ON pipeline_executions (project_id, started_at DESC);

-- Stage logs: replay one stage's output for one run, in order
CREATE INDEX IF NOT EXISTS idx_stage_logs_run_stage
    ON stage_logs (run_id, stage_name, created_at);

-- Audit ledger: list by tenant + timestamp (for SOC 2 export)
CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts
    ON audit_ledger (tenant_id, timestamp DESC);

-- Cost analysis: latest cost record per run
CREATE INDEX IF NOT EXISTS idx_cost_run
    ON cost_analysis (pipeline_run_id, computed_at DESC);

-- JSONB GIN index on verification evidence (for fast filtering/reporting)
CREATE INDEX IF NOT EXISTS idx_verification_evidence_gin
    ON verification_records USING GIN (evidence);

-- ─────────────────────────────────────────────────────────────────
-- APPLICATION ROLE — non-superuser that FastAPI connects as
-- Superusers bypass RLS; the app must use this role for tenant isolation
-- to be enforced. The `platform` superuser is for migrations only.
-- ─────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'app_password' NOSUPERUSER;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE platform TO app_user;
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
