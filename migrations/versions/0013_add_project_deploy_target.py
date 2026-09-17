"""Module 8 — real gap found live: the platform could build and push a real
image to ECR, but had no way to actually RUN it anywhere but the local Kind
cluster. `deploy_target` records which real deployment target a project
uses ("kubernetes" or "aws_ecs") so the API can compute the right live_url
and route future rollouts correctly. Defaults to "kubernetes" so every
existing project (and every caller that doesn't send this field) is
completely unaffected.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS deploy_target TEXT NOT NULL DEFAULT 'kubernetes'")
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS deploy_target"))
