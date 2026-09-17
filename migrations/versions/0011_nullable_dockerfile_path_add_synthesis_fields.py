"""Real gap found live: `projects.dockerfile_path` was NOT NULL DEFAULT
'Dockerfile' — correct when a Dockerfile was the only supported build
method, but the new build-detection scanner (shared/repo_scanner.py) can
now correctly determine a project has NO Dockerfile at all and should be
built via language/startCommand synthesis instead (dockerfile_synthesis.py).
Storing that decision required `dockerfile_path` to genuinely be nullable,
not a magic empty string standing in for "none" alongside a NOT NULL
constraint that would just reject it.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN dockerfile_path DROP NOT NULL"))
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN dockerfile_path DROP DEFAULT"))
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS language TEXT"))
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS start_command TEXT"))
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS manifest_path TEXT"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS manifest_path"))
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS start_command"))
    op.execute(sa.text("ALTER TABLE projects DROP COLUMN IF EXISTS language"))
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN dockerfile_path SET DEFAULT 'Dockerfile'"))
    op.execute(
        sa.text(
            "UPDATE projects SET dockerfile_path = 'Dockerfile' WHERE dockerfile_path IS NULL"
        )
    )
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN dockerfile_path SET NOT NULL"))
