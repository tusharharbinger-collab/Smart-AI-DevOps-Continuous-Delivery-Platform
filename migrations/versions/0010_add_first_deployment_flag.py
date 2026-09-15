"""Real gap found live (2026-09-15): onboarding always created a `baseline`
Deployment pinned to `active_production_tag` (default v1.0.0) and a
`canary` Deployment pinned to `canary_tag` (v1.1.0) as if two already-built
versions existed before onboarding even happened. A project with only ONE
version ever built (a genuinely first-time deployment) got a baseline
Deployment referencing a tag that was never pushed, and the pod failed
immediately with InvalidImageName/ImagePullBackOff.

Matches Argo Rollouts' own documented behavior: the first deployment for a
project is never a canary — there's nothing to compare it against yet, so
it should ship straight to 100% on both sides. `pipeline_id` (not
`project_id`) carries this flag since a Project wraps exactly one Pipeline
1:1 (see CLAUDE.md invariant 8) and pipeline-worker already has
`pipeline_id` in scope everywhere it would need to check this — no new
threading required.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS first_deployment_completed_at TIMESTAMPTZ")
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE pipelines DROP COLUMN IF EXISTS first_deployment_completed_at"))
