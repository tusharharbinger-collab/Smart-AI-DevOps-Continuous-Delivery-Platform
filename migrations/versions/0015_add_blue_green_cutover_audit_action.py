"""Guaranteed Live Web App CI/CD — real gap found live: the audit_ledger
CHECK constraint never included 'BLUE_GREEN_CUTOVER' at all, even in the
original (dormant) verdict-gated blue-green path built before this session —
any real call to record_actuation(action="BLUE_GREEN_CUTOVER") would fail
the constraint, caught by record_actuation's own try/except and logged as
db_audit_record_failed rather than raised, so the gap was invisible. Found
live only because worker.py's new health-gated blue-green path was wired up
to actually write real audit rows for the first time (0 rows previously
existed for a real blue-green cutover on any database).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE audit_ledger DROP CONSTRAINT IF EXISTS audit_ledger_action_check"))
    op.execute(
        sa.text(
            """
            ALTER TABLE audit_ledger ADD CONSTRAINT audit_ledger_action_check
                CHECK (action IN ('WEIGHT_UPDATE','ROLLBACK','PROMOTE','SCALE_ZERO',
                                   'APPROVE','BLOCK','RIGHTSIZING','GRADUATE','BLUE_GREEN_CUTOVER'))
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
                                   'APPROVE','BLOCK','RIGHTSIZING','GRADUATE'))
            """
        )
    )
