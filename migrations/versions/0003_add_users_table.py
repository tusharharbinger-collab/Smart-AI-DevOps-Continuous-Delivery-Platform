"""Add users table for real (signed) authentication.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id     UUID        NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                email         TEXT        NOT NULL UNIQUE,
                password_hash TEXT        NOT NULL,
                role          TEXT        NOT NULL DEFAULT 'developer'
                                  CHECK (role IN ('developer', 'lead-sre', 'platform-admin')),
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON users TO app_user"))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS users CASCADE"))
