"""Phase 9: real canary graduation (baseline image genuinely swapped to the
promoted version, not just a database label) needs a distinct audit_ledger
action — the existing CHECK constraint only allowed
('WEIGHT_UPDATE','ROLLBACK','PROMOTE','SCALE_ZERO','APPROVE','BLOCK',
'RIGHTSIZING'), so actuation_executor.py's graduate_canary() recording
action="GRADUATE" would fail the constraint on any database created before
this migration.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE audit_ledger DROP CONSTRAINT IF EXISTS audit_ledger_action_check"))
    op.execute(
        sa.text(
            """
            ALTER TABLE audit_ledger ADD CONSTRAINT audit_ledger_action_check
                CHECK (action IN ('WEIGHT_UPDATE','ROLLBACK','PROMOTE','SCALE_ZERO',
                                   'APPROVE','BLOCK','RIGHTSIZING','GRADUATE'))
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE audit_ledger DROP CONSTRAINT IF EXISTS audit_ledger_action_check"))
    op.execute(
        sa.text(
            """
            ALTER TABLE audit_ledger ADD CONSTRAINT audit_ledger_action_check
                CHECK (action IN ('WEIGHT_UPDATE','ROLLBACK','PROMOTE','SCALE_ZERO',
                                   'APPROVE','BLOCK','RIGHTSIZING'))
            """
        )
    )
