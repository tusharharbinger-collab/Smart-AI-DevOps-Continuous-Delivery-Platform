"""Real gap found live: `path_prefix` was collected at onboarding (routed
into the generated HTTPRoute) and then thrown away — never persisted on the
`projects` row, never returned by any API, so the platform never had a way
to show a user a real, clickable "is my product live" link for their own
deployed service (the way Render/Vercel do). Storing it is what lets the
API compute a real live_url.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS path_prefix TEXT"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS path_prefix"))
