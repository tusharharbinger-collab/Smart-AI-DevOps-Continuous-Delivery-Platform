"""Phase 4 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5/§6): the Infra Architect
Agent's state machine. A new dedicated table, not a column on `projects` —
this is pre-provisioning state (FORM_COLLECTION through INFRA_APPROVED)
with no `pipeline_run_id` and often no real `projects` row yet either,
since the Requirements Form flow can run before a project is ever created
via the existing POST /api/v1/projects. `project_id` is therefore nullable
by design, matching how `projects.repo_url`/`container_image` are already
nullable for an analogous "genuinely doesn't exist yet" reason (see
CLAUDE.md's note on adopted pipelines).

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS infra_build_state (
                draft_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id         UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                project_id        UUID        REFERENCES projects(project_id) ON DELETE CASCADE,
                status            TEXT        NOT NULL DEFAULT 'INTENT_LOCKED'
                                      CHECK (status IN ('INTENT_LOCKED', 'INFRA_DRAFTING',
                                                        'INFRA_DRAFT_FAILED', 'INFRA_PENDING_APPROVAL',
                                                        'INFRA_APPROVED')),
                intent_spec       JSONB       NOT NULL,
                archetype         TEXT,
                infra_proposal    JSONB,
                readiness_outcome TEXT,
                readiness_reasons JSONB       NOT NULL DEFAULT '[]'::jsonb,
                error_message     TEXT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.execute(sa.text("ALTER TABLE infra_build_state ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE infra_build_state FORCE ROW LEVEL SECURITY"))
    # PERMISSIVE (no `AS RESTRICTIVE`) — a RESTRICTIVE-only policy with no
    # companion PERMISSIVE one makes Postgres deny every row to everyone
    # (invariant 6 / CLAUDE.md's own documented trap).
    op.execute(
        sa.text(
            """
            DROP POLICY IF EXISTS tenant_isolation_infra_build_state ON infra_build_state;
            CREATE POLICY tenant_isolation_infra_build_state ON infra_build_state
                FOR ALL
                USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid)
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_infra_build_state_tenant_created "
            "ON infra_build_state (tenant_id, created_at DESC)"
        )
    )
    # app_user is created in schema.sql with grants on ALL TABLES as they
    # existed at that moment; a table added later by a migration needs its
    # own grant or every RLS-scoped query from the app 403s at the
    # permission layer before RLS is ever consulted.
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON infra_build_state TO app_user"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS infra_build_state"))
