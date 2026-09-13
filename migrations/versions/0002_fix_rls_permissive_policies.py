"""Fix RLS policies: RESTRICTIVE-only -> PERMISSIVE.

0001 created every tenant_isolation_* policy as `AS RESTRICTIVE`. A
RESTRICTIVE policy only narrows rows already granted by a PERMISSIVE
policy — with no PERMISSIVE policy on a table, Postgres denies every row
to everyone (including the tenant that legitimately owns them). The
practical effect was: a superuser connection (RLS-bypassing) saw every
tenant's rows, and a correctly non-superuser app connection saw zero rows,
ever. This migration drops and recreates each policy as PERMISSIVE (the
default — dropping `AS RESTRICTIVE`), which is what the RLS design
actually calls for: allow a row only when it belongs to the active tenant.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICIES = [
    ("tenant_isolation_pipelines", "pipelines"),
    ("tenant_isolation_executions", "pipeline_executions"),
    ("tenant_isolation_execstate", "execution_state"),
    ("tenant_isolation_verification", "verification_records"),
    ("tenant_isolation_policy", "policy_rules"),
    ("tenant_isolation_audit", "audit_ledger"),
    ("tenant_isolation_approvals", "approvals"),
    ("tenant_isolation_cost", "cost_analysis"),
]


def upgrade() -> None:
    for policy_name, table_name in _POLICIES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}"))
        op.execute(
            sa.text(
                f"""
                CREATE POLICY {policy_name} ON {table_name}
                    FOR ALL
                    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid)
                """
            )
        )


def downgrade() -> None:
    """Restores the original (broken) RESTRICTIVE-only policies for parity with 0001."""
    for policy_name, table_name in _POLICIES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}"))
        op.execute(
            sa.text(
                f"""
                CREATE POLICY {policy_name} ON {table_name}
                    AS RESTRICTIVE
                    FOR ALL
                    USING (tenant_id = current_setting('app.active_tenant_id', true)::uuid)
                """
            )
        )
