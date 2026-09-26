"""Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md): extends
`infra_build_state` (migration 0016) with the real-provisioning-execution
columns and status values — CloudFormation Change Set id/stack identity,
the cloud provider (a real column from day one, not hardcoded, so a future
second provider is a data value rather than a schema migration), and a
provisioning-specific error message distinct from the existing
infra-drafting error_message column.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE infra_build_state DROP CONSTRAINT IF EXISTS infra_build_state_status_check"))
    op.execute(
        sa.text(
            """
            ALTER TABLE infra_build_state ADD CONSTRAINT infra_build_state_status_check
                CHECK (status IN ('INTENT_LOCKED', 'INFRA_DRAFTING', 'INFRA_DRAFT_FAILED',
                                  'INFRA_PENDING_APPROVAL', 'INFRA_APPROVED',
                                  'INFRA_CHANGE_SET_CREATING', 'INFRA_CHANGE_SET_READY',
                                  'INFRA_CHANGE_SET_FAILED', 'INFRA_EXECUTION_APPROVED',
                                  'INFRA_PROVISIONING', 'INFRA_PROVISIONED',
                                  'INFRA_PROVISIONING_FAILED'))
            """
        )
    )
    for column_ddl in (
        "ADD COLUMN IF NOT EXISTS cloud_provider TEXT NOT NULL DEFAULT 'aws'",
        "ADD COLUMN IF NOT EXISTS change_set_id TEXT",
        "ADD COLUMN IF NOT EXISTS stack_name TEXT",
        "ADD COLUMN IF NOT EXISTS stack_arn TEXT",
        "ADD COLUMN IF NOT EXISTS change_set_changes JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ADD COLUMN IF NOT EXISTS provisioning_error TEXT",
        "ADD COLUMN IF NOT EXISTS provisioning_outputs JSONB NOT NULL DEFAULT '{}'::jsonb",
    ):
        op.execute(sa.text(f"ALTER TABLE infra_build_state {column_ddl}"))


def downgrade() -> None:
    # Must reset any row already in a Phase-7-only status BEFORE re-adding
    # the narrower constraint, or the ALTER TABLE itself fails validating
    # existing rows against the constraint being added.
    op.execute(sa.text("UPDATE infra_build_state SET status = 'INFRA_APPROVED' WHERE status NOT IN ("
                        "'INTENT_LOCKED', 'INFRA_DRAFTING', 'INFRA_DRAFT_FAILED', "
                        "'INFRA_PENDING_APPROVAL', 'INFRA_APPROVED')"))
    op.execute(sa.text("ALTER TABLE infra_build_state DROP CONSTRAINT IF EXISTS infra_build_state_status_check"))
    op.execute(
        sa.text(
            """
            ALTER TABLE infra_build_state ADD CONSTRAINT infra_build_state_status_check
                CHECK (status IN ('INTENT_LOCKED', 'INFRA_DRAFTING', 'INFRA_DRAFT_FAILED',
                                  'INFRA_PENDING_APPROVAL', 'INFRA_APPROVED'))
            """
        )
    )
    for column in (
        "cloud_provider", "change_set_id", "stack_name", "stack_arn",
        "change_set_changes", "provisioning_error", "provisioning_outputs",
    ):
        op.execute(sa.text(f"ALTER TABLE infra_build_state DROP COLUMN IF EXISTS {column}"))
