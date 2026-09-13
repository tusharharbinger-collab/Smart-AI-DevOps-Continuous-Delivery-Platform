"""Phase 4 (security hardening): auth_events audit table for login/refresh/
logout events, queryable rather than stdout-only. Refresh tokens themselves
are NOT stored in Postgres — the revocation list lives in Redis (a short-TTL
store is the right fit for token lifetimes, and avoids a DB round-trip on
every authenticated request).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS auth_events (
                event_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id   UUID        REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                user_id     UUID        REFERENCES users(user_id) ON DELETE SET NULL,
                email       TEXT        NOT NULL,
                event_type  TEXT        NOT NULL
                                CHECK (event_type IN (
                                    'LOGIN_SUCCESS', 'LOGIN_FAILURE', 'LOGIN_LOCKED_OUT',
                                    'TOKEN_REFRESH', 'TOKEN_REFRESH_REJECTED', 'LOGOUT'
                                )),
                ip_address  TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    # No RLS: same reasoning as `users` (schema.sql) — a failed login with a
    # bad/unknown email has no tenant_id to scope by, and nothing sensitive
    # beyond an email + timestamp lives here.
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON auth_events TO app_user"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_auth_events_email_created "
            "ON auth_events (email, created_at DESC)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS auth_events CASCADE"))
