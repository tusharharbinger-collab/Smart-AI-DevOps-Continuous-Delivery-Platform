# services/api-gateway/src/db/session.py
"""
Async SQLAlchemy engine + session factory.

Route handlers must use the `get_request_db` dependency, NOT `get_db`.
`get_db` opens a brand new pooled connection unrelated to the one
auth/middleware.py set `app.active_tenant_id` on — with NullPool that is a
genuinely different physical Postgres connection, so any RLS context set on
one is invisible to the other and every RLS-scoped query would silently see
zero rows (or, worse, all rows if it happened to reuse a connection last
scoped to a different tenant). `get_request_db` instead returns the exact
session middleware already opened and scoped for this request
(`request.state.db`), so RLS is guaranteed to apply. `get_db` is kept only
for callers that manage their own tenant context explicitly (none currently).

Connects via APP_POSTGRES_DSN (the non-superuser `app_user` role created in
db/schema.sql), never POSTGRES_DSN (the `platform` superuser used only for
migrations) — a superuser connection unconditionally bypasses Row-Level
Security regardless of FORCE ROW LEVEL SECURITY, which would silently
defeat every tenant_isolation_* policy.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from fastapi import Request
import os

POSTGRES_DSN = os.environ.get(
    "APP_POSTGRES_DSN",
    os.environ.get(
        "POSTGRES_DSN",
        "postgresql+asyncpg://app_user:app_password@localhost:5432/platform",
    ),
)

# NullPool is recommended for applications with many short-lived connections
# (e.g. serverless, worker pools). For long-running services, use AsyncAdaptedQueuePool.
engine = create_async_engine(
    POSTGRES_DSN,
    echo=False,         # set to True to log all SQL (dev only)
    pool_pre_ping=True,
    poolclass=NullPool,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncSession:
    """
    Opens a brand new session with no RLS tenant context set. Do NOT use this
    in route handlers that query tenant-scoped tables — see module docstring.
    """
    async with AsyncSessionLocal() as session:
        yield session


def get_request_db(request: Request) -> AsyncSession:
    """
    FastAPI dependency for route handlers: returns the session
    auth/middleware.py already opened and scoped with
    `SET LOCAL app.active_tenant_id` for this request, so RLS policies see
    the correct tenant on every query a route handler runs.
    """
    return request.state.db
