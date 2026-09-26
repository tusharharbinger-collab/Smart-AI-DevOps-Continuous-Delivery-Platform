"""AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md Phase A: `source` (ai_created |
existing), the human's picked `existing_resources` slot->identifier map, and
`parent_draft_id` (an edit is a NEW row pointing at the draft it revised, so
edit history is an audit trail, never an in-place overwrite).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE infra_build_state ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ai_created'"))
    op.execute(sa.text("ALTER TABLE infra_build_state DROP CONSTRAINT IF EXISTS infra_build_state_source_check"))
    op.execute(sa.text("ALTER TABLE infra_build_state ADD CONSTRAINT infra_build_state_source_check CHECK (source IN ('ai_created', 'existing'))"))
    op.execute(sa.text("ALTER TABLE infra_build_state ADD COLUMN IF NOT EXISTS existing_resources JSONB NOT NULL DEFAULT '{}'::jsonb"))
    op.execute(sa.text("ALTER TABLE infra_build_state ADD COLUMN IF NOT EXISTS parent_draft_id UUID REFERENCES infra_build_state(draft_id) ON DELETE SET NULL"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE infra_build_state DROP COLUMN IF EXISTS parent_draft_id"))
    op.execute(sa.text("ALTER TABLE infra_build_state DROP COLUMN IF EXISTS existing_resources"))
    op.execute(sa.text("ALTER TABLE infra_build_state DROP CONSTRAINT IF EXISTS infra_build_state_source_check"))
    op.execute(sa.text("ALTER TABLE infra_build_state DROP COLUMN IF EXISTS source"))
