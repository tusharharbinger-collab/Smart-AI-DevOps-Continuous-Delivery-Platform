"""
services/api-gateway/src/routers/auth_router.py

Real (signed) authentication, hardened in Phase 4 (§04-security-hardening.md,
deliverable 4.2):
  - Redis-backed sliding-window lockout on repeated failed logins.
  - Short-lived (15 min) access tokens + a longer-lived (7 day) opaque
    refresh token, held in Redis as an allow-list — the allow-list doubles
    as the revocation mechanism (delete the key to revoke; logout does
    exactly that), which a stateless JWT alone can never support.
  - Every login/refresh/logout event is written to `auth_events`, queryable
    (unlike stdout-only logs).

Login intentionally runs against `get_db()` (the plain, non-tenant-scoped
session) rather than `get_request_db()` — there is no tenant context yet at
login time (we don't know which tenant until we've found the user by
email), and `users`/`auth_events` deliberately have no RLS policy for
exactly this reason (see db/schema.sql).
"""
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.config import settings
from src.db.session import get_db

router = APIRouter()
logger = structlog.get_logger(__name__)

ACCESS_TOKEN_TTL_MINUTES = 15
REFRESH_TOKEN_TTL_SECONDS = 7 * 24 * 3600

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_WINDOW_SECONDS = 300


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    tenant_id: str
    role: str
    email: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutRequest(BaseModel):
    refresh_token: str


def _issue_access_token(user_id: str, email: str, tenant_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "email": email,
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES),
    }
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm="HS256")


async def _issue_refresh_token(request: Request, user_id: str, email: str, tenant_id: str, role: str) -> str:
    """
    An opaque random token (not a JWT) stored in Redis as the source of
    truth: `refresh_token:{token}` -> the claims needed to mint a new access
    token, with a TTL that IS the token's lifetime. Because possession of a
    valid key is what makes the token valid, deleting the key immediately
    and unconditionally revokes it — the property a self-verifying JWT
    cannot give you without a separate deny-list.
    """
    token = secrets.token_urlsafe(32)
    await request.app.state.redis.set(
        f"refresh_token:{token}",
        f"{user_id}|{email}|{tenant_id}|{role}",
        ex=REFRESH_TOKEN_TTL_SECONDS,
    )
    return token


async def _record_auth_event(
    db: AsyncSession,
    event_type: str,
    email: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO auth_events (tenant_id, user_id, email, event_type, ip_address)
            VALUES (:tenant_id, :user_id, :email, :event_type, :ip_address)
            """
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "email": email,
            "event_type": event_type,
            "ip_address": ip_address,
        },
    )
    await db.commit()


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Phase 5 (§05-reliability-scale.md, deliverable 5.4) — an explicit,
    tested decision about what happens when Redis is unreachable, found by
    actually stopping Redis and calling this endpoint (it 500'd with a raw
    traceback before this fix): rate limiting is a defensive ADD-ON, not
    core functionality, so a Redis outage makes it fail OPEN (skip the
    check, log a warning, let the login attempt proceed) rather than
    blocking every single login in the system because a secondary control
    became unavailable. Refresh-token issuance, by contrast, is a genuine,
    intentional hard dependency on Redis (Phase 4's revocable-token design)
    — that failure is NOT gracefully degraded, but it is now a clean,
    intentional 503 instead of an unhandled 500 traceback.
    """
    redis_client = request.app.state.redis
    client_ip = request.client.host if request.client else None
    attempts_key = f"login_attempts:{body.email}"

    try:
        attempts = await redis_client.get(attempts_key)
        if attempts is not None and int(attempts) >= LOGIN_MAX_ATTEMPTS:
            await _record_auth_event(db, "LOGIN_LOCKED_OUT", body.email, ip_address=client_ip)
            logger.warning("login_locked_out", email=body.email)
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Try again in a few minutes.",
            )
        rate_limiting_available = True
    except RedisError as e:
        logger.warning("rate_limit_check_skipped_redis_unavailable", error=str(e))
        rate_limiting_available = False

    result = await db.execute(
        text("SELECT user_id, tenant_id, email, password_hash, role FROM users WHERE email = :email"),
        {"email": body.email},
    )
    user = result.mappings().first()

    # Constant-shape failure: a bad email and a bad password both just 401 —
    # never reveal which one was wrong.
    if user is None or not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        if rate_limiting_available:
            try:
                # incr-then-expire (not `set ... ex` on the first failure) so
                # the lockout window is anchored to the FIRST failed attempt,
                # not reset by every subsequent one — otherwise a slow-drip
                # attacker who never pauses would never actually get locked out.
                new_count = await redis_client.incr(attempts_key)
                if new_count == 1:
                    await redis_client.expire(attempts_key, LOGIN_LOCKOUT_WINDOW_SECONDS)
            except RedisError as e:
                logger.warning("rate_limit_increment_skipped_redis_unavailable", error=str(e))
        await _record_auth_event(db, "LOGIN_FAILURE", body.email, ip_address=client_ip)
        logger.warning("login_failed", email=body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if rate_limiting_available:
        try:
            await redis_client.delete(attempts_key)
        except RedisError as e:
            logger.warning("rate_limit_reset_skipped_redis_unavailable", error=str(e))

    access_token = _issue_access_token(str(user["user_id"]), user["email"], str(user["tenant_id"]), user["role"])
    try:
        refresh_token = await _issue_refresh_token(
            request, str(user["user_id"]), user["email"], str(user["tenant_id"]), user["role"]
        )
    except RedisError as e:
        logger.error("login_failed_redis_unavailable", email=user["email"], error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Authentication service temporarily unavailable — please try again shortly.",
        )

    await _record_auth_event(
        db, "LOGIN_SUCCESS", user["email"], tenant_id=str(user["tenant_id"]), user_id=str(user["user_id"]),
        ip_address=client_ip,
    )
    logger.info("login_succeeded", email=user["email"], tenant_id=str(user["tenant_id"]))
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        tenant_id=str(user["tenant_id"]),
        role=user["role"],
        email=user["email"],
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(body: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Exchanges a valid refresh token for a new (short-lived) access token.
    Rotates the refresh token on every use — the old one is deleted and a
    new one issued — so a refresh token is single-use-per-rotation-window,
    shrinking the blast radius of one being copied off a device.
    """
    redis_client = request.app.state.redis
    key = f"refresh_token:{body.refresh_token}"
    raw = await redis_client.get(key)
    if raw is None:
        await _record_auth_event(db, "TOKEN_REFRESH_REJECTED", "unknown")
        logger.warning("refresh_token_invalid_or_expired")
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired")

    user_id, email, tenant_id, role = raw.split("|", 3)
    await redis_client.delete(key)

    access_token = _issue_access_token(user_id, email, tenant_id, role)
    new_refresh_token = await _issue_refresh_token(request, user_id, email, tenant_id, role)

    await _record_auth_event(db, "TOKEN_REFRESH", email, tenant_id=tenant_id, user_id=user_id)
    logger.info("token_refreshed", email=email, tenant_id=tenant_id)
    return RefreshResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout")
async def logout(body: LogoutRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Revokes the refresh token immediately — deleting its Redis key is the revocation."""
    redis_client = request.app.state.redis
    key = f"refresh_token:{body.refresh_token}"
    raw = await redis_client.get(key)
    if raw is not None:
        user_id, email, tenant_id, _role = raw.split("|", 3)
        await redis_client.delete(key)
        await _record_auth_event(db, "LOGOUT", email, tenant_id=tenant_id, user_id=user_id)
        logger.info("logout_succeeded", email=email)
    return {"status": "logged_out"}
