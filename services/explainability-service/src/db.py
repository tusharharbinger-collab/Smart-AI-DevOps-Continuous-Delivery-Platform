"""
services/explainability-service/src/db.py

Minimal async Postgres access for this service — read-only queries needed by
digest_generator.py. Mirrors api-gateway/src/db/session.py's engine config
without duplicating the full ORM (this service only ever reads aggregates).

Connects via APP_POSTGRES_DSN (the non-superuser `app_user` role from
db/schema.sql) so RLS actually scopes the digest to the requested tenant —
see api-gateway/src/db/session.py for why the superuser DSN must never be
used for request-serving queries.
"""
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

POSTGRES_DSN = os.environ.get(
    "APP_POSTGRES_DSN",
    os.environ.get(
        "POSTGRES_DSN",
        "postgresql+asyncpg://app_user:app_password@localhost:5432/platform",
    ),
)

engine = create_async_engine(POSTGRES_DSN, echo=False, pool_pre_ping=True, poolclass=NullPool)
AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """
    Real bug found live: this never set `app.active_tenant_id`, so every RLS
    policy on the tables digest_generator.py queries (pipeline_executions,
    verification_records, cost_analysis) compared `tenant_id` against an
    unset session variable and matched nothing — the digest endpoint
    silently returned an empty/zero digest for every tenant, always, with no
    error. The explicit `WHERE tenant_id = :tenant_id` in each query never
    saved it, since FORCE ROW LEVEL SECURITY applies on top of that filter,
    not instead of it. This service has no per-request middleware (unlike
    api-gateway), so the caller sets tenant context itself right after
    opening the session — see main.py's `get_digest`.
    """
    async with AsyncSessionLocal() as session:
        yield session
