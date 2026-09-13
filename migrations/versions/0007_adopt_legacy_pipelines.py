"""Phase 8 follow-up: retire the classic /app console by making every
pipeline reachable through a project workspace.

Pipelines registered directly (POST /api/v1/pipelines, or the Phase 3
onboarding endpoint) have no project, and were previously only visible in
the classic console's pipeline picker. With that console removed they would
have become unreachable in the UI along with all their run history — so
this adopts each orphan pipeline into a project and back-fills `project_id`
onto its existing executions.

An adopted pipeline genuinely has no repository or image behind it, so
`repo_url` and `container_image` drop their NOT NULL constraints rather
than being filled with invented values. The UI renders those as
"No repository connected".

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN repo_url DROP NOT NULL"))
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN container_image DROP NOT NULL"))

    # Adopt every pipeline that has no project yet. The project takes the
    # pipeline's own name minus the "-rollout" suffix the generators append,
    # so an adopted `checkout-service-rollout` shows up as `checkout-service`.
    op.execute(
        sa.text(
            """
            INSERT INTO projects (tenant_id, pipeline_id, name, repo_url, container_image, status)
            SELECT p.tenant_id,
                   p.pipeline_id,
                   regexp_replace(p.name, '-rollout$', ''),
                   NULL,
                   NULL,
                   'IDLE'
            FROM pipelines p
            WHERE NOT EXISTS (SELECT 1 FROM projects pr WHERE pr.pipeline_id = p.pipeline_id)
              AND NOT EXISTS (
                  SELECT 1 FROM projects pr2
                  WHERE pr2.tenant_id = p.tenant_id
                    AND pr2.name = regexp_replace(p.name, '-rollout$', '')
              )
            """
        )
    )

    # Attach existing runs to the project that now owns their pipeline, so a
    # workspace shows the full history rather than starting empty.
    op.execute(
        sa.text(
            """
            UPDATE pipeline_executions e
            SET project_id = pr.project_id
            FROM projects pr
            WHERE pr.pipeline_id = e.pipeline_id
              AND pr.tenant_id = e.tenant_id
              AND e.project_id IS NULL
            """
        )
    )


def downgrade() -> None:
    # Only detach/remove the adopted rows (those with no repository), never a
    # project that was genuinely created through the wizard.
    op.execute(
        sa.text(
            """
            UPDATE pipeline_executions e
            SET project_id = NULL
            FROM projects pr
            WHERE pr.project_id = e.project_id AND pr.repo_url IS NULL
            """
        )
    )
    op.execute(sa.text("DELETE FROM projects WHERE repo_url IS NULL"))
