"""Initial schema: all tables, RLS policies, and indexes.

This migration applies the complete schema defined in
services/api-gateway/src/db/schema.sql by reading it from disk and
executing it directly — this guarantees the migration and the canonical
schema file stay in sync.

Revision ID: 0001
Revises: 
Create Date: 2026-09-12

"""
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Resolve schema.sql relative to this file
_SCHEMA_SQL = (
    Path(__file__).parent.parent.parent
    / "services" / "api-gateway" / "src" / "db" / "schema.sql"
)


def upgrade() -> None:
    """Apply the complete schema from schema.sql."""
    schema_sql = _SCHEMA_SQL.read_text(encoding="utf-8")
    
    # Execute as a single statement block — PostgreSQL handles IF NOT EXISTS
    # so running this against an existing DB with the same schema is idempotent.
    op.execute(sa.text(schema_sql))


def downgrade() -> None:
    """Drop all tables in dependency order (children first)."""
    op.execute(sa.text("""
        DROP TABLE IF EXISTS cost_analysis CASCADE;
        DROP TABLE IF EXISTS approvals CASCADE;
        DROP TABLE IF EXISTS audit_ledger CASCADE;
        DROP TABLE IF EXISTS policy_rules CASCADE;
        DROP TABLE IF EXISTS verification_records CASCADE;
        DROP TABLE IF EXISTS execution_state CASCADE;
        DROP TABLE IF EXISTS pipeline_executions CASCADE;
        DROP TABLE IF EXISTS pipelines CASCADE;
        DROP TABLE IF EXISTS tenants CASCADE;
        DROP EXTENSION IF EXISTS pgcrypto;
    """))
