"""Phase 8 (project workspaces): a `projects` table that wraps an existing
`pipelines` row, git/trigger provenance on `pipeline_executions`, and a
durable `stage_logs` table.

Deliberately additive: `projects` OWNS a pipeline rather than replacing it,
and every new column on `pipeline_executions` is nullable, so existing rows
and every non-project pipeline run stay valid. See
docs/roadmap/08-project-workspaces.md for why a parallel runs table would
have orphaned verification_records/audit_ledger (both FK pipeline_run_id).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id             UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                pipeline_id           UUID        REFERENCES pipelines(pipeline_id) ON DELETE SET NULL,
                name                  TEXT        NOT NULL,
                repo_url              TEXT        NOT NULL,
                branch                TEXT        NOT NULL DEFAULT 'main',
                root_directory        TEXT        NOT NULL DEFAULT './',
                dockerfile_path       TEXT        NOT NULL DEFAULT 'Dockerfile',
                test_command          TEXT,
                container_image       TEXT        NOT NULL,
                active_production_tag TEXT        NOT NULL DEFAULT 'v1.0.0',
                canary_tag            TEXT,
                status                TEXT        NOT NULL DEFAULT 'IDLE'
                                          CHECK (status IN ('IDLE','BUILDING','TESTING','VERIFYING',
                                                            'HEALTHY','ROLLED_BACK','FAILED')),
                created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (tenant_id, name)
            )
            """
        )
    )
    op.execute(sa.text("ALTER TABLE projects ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE projects FORCE ROW LEVEL SECURITY"))
    # PERMISSIVE (no `AS RESTRICTIVE`) — a RESTRICTIVE-only policy with no
    # companion PERMISSIVE one makes Postgres deny every row to everyone.
    op.execute(
        sa.text(
            """
            DROP POLICY IF EXISTS tenant_isolation_projects ON projects;
            CREATE POLICY tenant_isolation_projects ON projects
                FOR ALL
                USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid)
            """
        )
    )

    for column_ddl in (
        "ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE",
        "ADD COLUMN IF NOT EXISTS trigger_type TEXT",
        "ADD COLUMN IF NOT EXISTS commit_sha TEXT",
        "ADD COLUMN IF NOT EXISTS commit_message TEXT",
    ):
        op.execute(sa.text(f"ALTER TABLE pipeline_executions {column_ddl}"))

    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS stage_logs (
                id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                run_id      UUID        NOT NULL REFERENCES pipeline_executions(pipeline_run_id) ON DELETE CASCADE,
                tenant_id   UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                stage_name  TEXT        NOT NULL,
                content     TEXT        NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.execute(sa.text("ALTER TABLE stage_logs ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE stage_logs FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DROP POLICY IF EXISTS tenant_isolation_stage_logs ON stage_logs;
            CREATE POLICY tenant_isolation_stage_logs ON stage_logs
                FOR ALL
                USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid)
            """
        )
    )

    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_projects_tenant_created "
            "ON projects (tenant_id, created_at DESC)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_executions_project_started "
            "ON pipeline_executions (project_id, started_at DESC)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_stage_logs_run_stage "
            "ON stage_logs (run_id, stage_name, created_at)"
        )
    )

    # app_user is created in schema.sql with grants on ALL TABLES as they
    # existed at that moment; tables added later by a migration need their
    # own grant or every RLS-scoped query from the app 403s at the
    # permission layer before RLS is ever consulted.
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON projects TO app_user"))
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON stage_logs TO app_user"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS stage_logs"))
    for column in ("project_id", "trigger_type", "commit_sha", "commit_message"):
        op.execute(sa.text(f"ALTER TABLE pipeline_executions DROP COLUMN IF EXISTS {column}"))
    op.execute(sa.text("DROP TABLE IF EXISTS projects"))
