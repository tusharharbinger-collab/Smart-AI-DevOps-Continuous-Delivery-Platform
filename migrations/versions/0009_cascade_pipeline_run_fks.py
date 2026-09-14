"""Real bug found live: DELETE /api/v1/projects/{id} deletes from `projects`,
which is supposed to cascade to `pipeline_executions` (ON DELETE CASCADE)
and, with it, everything scoped to a run. `execution_state` and
`stage_logs` correctly cascade from `pipeline_executions.pipeline_run_id`,
but `verification_records`, `audit_ledger`, `approvals`, and
`cost_analysis` never did — so deleting any project that had ever produced
a real verdict, audit record, approval, or cost row failed outright with a
ForeignKeyViolationError, a 500, and the project left half-deleted needing
a manual rollback. Caught only by actually deleting a project with real
run history, not by any test, since nothing exercised delete_project
against a project with rows in these tables.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ["verification_records", "audit_ledger", "approvals", "cost_analysis"]


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pipeline_run_id_fkey"
            )
        )
        op.execute(
            sa.text(
                f"""
                ALTER TABLE {table} ADD CONSTRAINT {table}_pipeline_run_id_fkey
                    FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_executions(pipeline_run_id)
                    ON DELETE CASCADE
                """
            )
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pipeline_run_id_fkey"
            )
        )
        op.execute(
            sa.text(
                f"""
                ALTER TABLE {table} ADD CONSTRAINT {table}_pipeline_run_id_fkey
                    FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_executions(pipeline_run_id)
                """
            )
        )
