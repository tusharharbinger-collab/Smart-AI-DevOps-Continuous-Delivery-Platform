"""Guaranteed Live Web App CI/CD — a genuinely new web app with zero real
visitors can never accumulate the samples statistical canary verification
needs, so it could sit DEGRADED forever and never actually reach its live
URL. `deploy_mode` lets a project opt into blue-green (instant, health-
gated cutover) instead of the statistically-verified canary ramp;
`live_url_status`/`live_url_verified_at` record the result of a real HTTP
GET against the live URL after every cutover (first deployment, blue-green,
canary graduation) — the platform's first *active proof* the URL actually
serves a response, not just that an ALB weight was flipped. All three
default/nullable so every existing project (and every caller that doesn't
send deploy_mode) is completely unaffected.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS deploy_mode TEXT NOT NULL DEFAULT 'canary'")
    )
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS live_url_status TEXT"))
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS live_url_verified_at TIMESTAMPTZ"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS live_url_verified_at"))
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS live_url_status"))
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS deploy_mode"))
