"""Phase 5 (reliability & scale): execution_state needs pipeline_id so
reconciler.py can look up the pipeline's registered manifest (policy_yaml)
to actually resume an interrupted run, rather than only knowing which run
and stage it was on with no way to re-fetch what that stage even means.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE execution_state ADD COLUMN IF NOT EXISTS pipeline_id UUID "
            "REFERENCES pipelines(pipeline_id)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE execution_state DROP COLUMN IF EXISTS pipeline_id"))
