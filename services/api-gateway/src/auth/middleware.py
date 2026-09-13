# services/api-gateway/src/auth/middleware.py
"""
Sets the RLS session variable (app.active_tenant_id) on every request,
inside the same transaction as the query — so PostgreSQL's RLS policies
always see the correct tenant_id and no row can leak across tenant boundaries.

The JWT is verified here with the same HS256 secret auth_router.py signs
with (`JWT_SECRET_KEY`) — a client cannot forge a `tenant_id` claim without
that secret. This used to decode with `verify_signature: False`, which meant
any client could hand-edit a token's `tenant_id` claim and read/write another
tenant's data; that was a real, working RLS bypass until this was fixed.
"""
import logging
import uuid
from typing import Callable

import jwt
from fastapi import Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def tenant_context_middleware(request: Request, call_next: Callable) -> Response:
    """
    Starlette middleware that:
    1. Extracts the tenant_id from the JWT Authorization header.
    2. Opens a DB session and runs `SET LOCAL app.active_tenant_id = '<tenant_id>'`
       so every SQL statement in this request is scoped to that tenant via RLS.
    3. Stores the session and tenant_id on request.state for use by route handlers.

    The `SET LOCAL` is transaction-scoped — it is automatically reset when the
    transaction ends, so there is no risk of it leaking to the next request.
    """
    # Skip auth for health/readiness endpoints and OpenAPI docs
    if request.url.path in (
        "/healthz", "/readyz", "/metrics", "/docs", "/openapi.json", "/redoc",
        "/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout",
        # Phase 8 — GitHub's OAuth redirect back to us. This is a top-level
        # browser navigation, so it cannot carry an Authorization header;
        # identity is re-established from the single-use `state` value the
        # router minted and stored in Redis, which is also what makes the
        # flow CSRF-resistant. See routers/github_router.py.
        "/api/v1/integrations/github/callback",
    ):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    claims = _extract_claims(auth_header)

    if claims is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid Authorization header"},
        )

    tenant_id = claims["tenant_id"]

    # Bind trace_id for structured logging (populated before call_next so all
    # route-handler log statements include it automatically)
    import structlog
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(trace_id=trace_id, tenant_id=str(tenant_id))

    request.state.tenant_id = tenant_id
    request.state.trace_id = trace_id
    # `role` backs the require_role() dependency (auth/rbac.py, Phase 4 —
    # the JWT carried this claim from day one, but nothing checked it before
    # this phase; every endpoint accepted any authenticated role equally.
    request.state.role = claims["role"]
    request.state.user_id = claims["user_id"]

    # Open a session and set the RLS variable for the duration of this request.
    # `SET LOCAL` does not accept a bind parameter (Postgres requires a literal
    # there, so `SET LOCAL app.active_tenant_id = :tid` is a syntax error at
    # execution time) — `set_config(..., true)` is the parameterized
    # equivalent of `SET LOCAL` and is what actually accepts a bound value.
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.active_tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            request.state.db = session
            response = await call_next(request)

    structlog.contextvars.clear_contextvars()
    return response


def _extract_claims(auth_header: str) -> dict | None:
    """
    Verifies the JWT's HS256 signature against JWT_SECRET_KEY (the same
    secret auth_router.py signs with at login) and returns the claims this
    middleware and the RBAC dependency (auth/rbac.py) need. Returns None if
    the header is absent, the signature doesn't verify, the token is
    expired, or a required claim is missing/malformed — all of which the
    middleware treats identically as "not authenticated".
    """
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_signature": True, "verify_exp": True},
        )
        raw_tenant = payload.get("tenant_id")
        role = payload.get("role")
        user_id = payload.get("sub")
        if raw_tenant is None or role is None or user_id is None:
            return None
        return {"tenant_id": uuid.UUID(str(raw_tenant)), "role": role, "user_id": user_id}
    except (jwt.PyJWTError, ValueError, AttributeError) as exc:
        logger.warning("jwt_verification_failed", extra={"error": str(exc)})
        return None
