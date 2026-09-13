"""
services/api-gateway/src/routers/github_router.py

Phase 8 (§08-project-workspaces.md, deliverable 8.2) — GitHub account
connection + repo ingestion backing the project-creation wizard.

Connection is a real OAuth 2.0 Authorization Code flow ("Connect GitHub",
the way Render does it), not a pasted personal access token:

  1. The signed-in user asks for an authorize URL. We mint a random `state`,
     store it in Redis bound to that user, and hand back GitHub's URL.
  2. The browser is redirected to GitHub and the user authorizes.
  3. GitHub redirects back to /callback with `code` + `state`. That request
     is a top-level browser navigation, so it carries NO Authorization
     header — the `state` lookup is what re-establishes which user this is,
     which is also exactly what makes the flow CSRF-resistant.
  4. We exchange the code for an access token server-side (the client
     secret never reaches the browser) and store the token in Redis keyed
     by user id, then bounce the browser back to the wizard.

Where the token lives: Redis, never Postgres. This service has no
`cryptography` dependency, so a DB column would mean a plaintext OAuth
token at rest in the tenant database; Redis matches how Phase 4 already
stores revocable refresh tokens, and a Redis restart simply means the user
reconnects. The token is never logged and never sent to the browser.

Token resolution order for API calls, most specific first:
  1. `X-GitHub-Token` request header (a caller supplying its own token)
  2. the signed-in user's stored OAuth token
  3. `GITHUB_TOKEN` configured on the gateway (shared fallback)
"""
import secrets
import urllib.parse

import httpx
import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from src.config import settings

router = APIRouter()
logger = structlog.get_logger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN = "https://github.com/login/oauth/access_token"

_TIMEOUT_SECONDS = 15.0
# Long enough that a connection survives a normal working session without
# re-authorizing; short enough that an abandoned token expires on its own.
_TOKEN_TTL_SECONDS = 30 * 24 * 3600
# One authorize round-trip; anything longer is a stale/replayed state.
_STATE_TTL_SECONDS = 600

# `scope=repo` is required to see PRIVATE repositories. A user who only
# needs public ones can authorize with less, but GitHub does not let the
# app narrow that per-user at request time — this is the scope the app asks
# for; the user sees and approves it on GitHub's own consent screen.
_OAUTH_SCOPE = "repo read:user"


def _token_key(user_id: str) -> str:
    return f"github:token:{user_id}"


def _profile_key(user_id: str) -> str:
    return f"github:profile:{user_id}"


def _state_key(state: str) -> str:
    return f"github:oauth_state:{state}"


def _require_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Missing user context")
    return str(user_id)


def _oauth_configured() -> bool:
    return bool(settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET)


async def _resolve_token(request: Request, header_token: str | None) -> str:
    if header_token and header_token.strip():
        return header_token.strip()

    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        stored = await request.app.state.redis.get(_token_key(str(user_id)))
        if stored:
            return stored

    if settings.GITHUB_TOKEN.strip():
        return settings.GITHUB_TOKEN.strip()

    raise HTTPException(
        status_code=400,
        detail=(
            "GitHub is not connected. Click 'Connect GitHub' to authorize your account, "
            "or paste a public Git clone URL instead."
        ),
    )


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _github_get(path: str, token: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.get(f"{GITHUB_API}{path}", headers=_github_headers(token), params=params)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")

    if resp.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="GitHub rejected the token (401). Reconnect your GitHub account.",
        )
    if resp.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail="GitHub returned 403 — the token lacks the required scope, or the rate limit is exhausted.",
        )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found on GitHub (or the token cannot see it).")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GitHub error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ─────────────────────────── connection lifecycle ───────────────────────────


@router.get("/status")
async def connection_status(request: Request):
    """
    Whether this user has connected GitHub, and who they connected as.
    Never returns the token itself — only the non-secret profile fields the
    wizard shows ("Connected as @octocat").
    """
    user_id = _require_user_id(request)
    redis_client = request.app.state.redis
    token = await redis_client.get(_token_key(user_id))
    profile_raw = await redis_client.get(_profile_key(user_id))

    profile = {}
    if profile_raw:
        import json

        try:
            profile = json.loads(profile_raw)
        except json.JSONDecodeError:
            profile = {}

    return {
        "connected": bool(token),
        "oauth_configured": _oauth_configured(),
        "server_token_fallback": bool(settings.GITHUB_TOKEN.strip()),
        "login": profile.get("login"),
        "avatar_url": profile.get("avatar_url"),
        "name": profile.get("name"),
    }


@router.get("/authorize-url")
async def authorize_url(request: Request):
    """
    Mints the GitHub authorize URL for this user.

    The `state` is random, single-use, and stored in Redis bound to this
    user id — it is both the CSRF defence and the only way /callback (an
    unauthenticated browser redirect) can tell whose token it is storing.
    """
    if not _oauth_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub OAuth is not configured on this server. Set GITHUB_CLIENT_ID and "
                "GITHUB_CLIENT_SECRET (register an OAuth App at GitHub → Settings → Developer "
                "settings → OAuth Apps, with callback URL "
                f"{settings.GITHUB_OAUTH_REDIRECT_URI})."
            ),
        )

    user_id = _require_user_id(request)
    state = secrets.token_urlsafe(32)
    await request.app.state.redis.set(_state_key(state), user_id, ex=_STATE_TTL_SECONDS)

    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        "scope": _OAUTH_SCOPE,
        "state": state,
        # Force the account picker so a user with several GitHub accounts
        # can choose, instead of silently reusing the browser's session.
        "allow_signup": "false",
    }
    return {"authorize_url": f"{GITHUB_OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"}


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    """
    GitHub's redirect target. Unauthenticated by design (see the allowlist in
    auth/middleware.py): a top-level browser navigation carries no
    Authorization header, so identity comes from the one-time `state`.

    Always ends in a redirect back to the wizard — never a raw JSON error —
    because the thing on the other end of this request is a browser window,
    not an API client.
    """

    def _back(status: str, reason: str | None = None) -> RedirectResponse:
        params = {"github": status}
        if reason:
            params["reason"] = reason[:200]
        return RedirectResponse(
            url=f"{settings.FRONTEND_BASE_URL}/projects/new?{urllib.parse.urlencode(params)}",
            status_code=302,
        )

    if error:
        logger.warning("github_oauth_denied", error=error)
        return _back("error", error_description or error)
    if not code or not state:
        return _back("error", "GitHub did not return an authorization code.")

    redis_client = request.app.state.redis
    user_id = await redis_client.get(_state_key(state))
    if not user_id:
        # Unknown/expired/replayed state — refuse rather than guess whose
        # account this token should be attached to.
        logger.warning("github_oauth_state_rejected")
        return _back("error", "Authorization expired or was already used. Please try again.")
    await redis_client.delete(_state_key(state))

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            token_resp = await client.post(
                GITHUB_OAUTH_TOKEN,
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "client_secret": settings.GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
                },
            )
        except httpx.RequestError as e:
            return _back("error", f"Could not reach GitHub: {e}")

    payload = token_resp.json() if token_resp.headers.get("content-type", "").startswith("application/json") else {}
    access_token = payload.get("access_token")
    if not access_token:
        # GitHub reports exchange failures with HTTP 200 and an `error` body,
        # so the status code alone is not enough to detect this.
        logger.warning("github_oauth_exchange_failed", error=payload.get("error"))
        return _back("error", payload.get("error_description") or "Token exchange failed.")

    await redis_client.set(_token_key(user_id), access_token, ex=_TOKEN_TTL_SECONDS)

    # Cache the non-secret profile so the wizard can say who is connected
    # without spending a GitHub API call on every page load.
    try:
        profile = await _github_get("/user", access_token)
        import json

        await redis_client.set(
            _profile_key(user_id),
            json.dumps(
                {
                    "login": profile.get("login"),
                    "avatar_url": profile.get("avatar_url"),
                    "name": profile.get("name"),
                }
            ),
            ex=_TOKEN_TTL_SECONDS,
        )
        logger.info("github_connected", user_id=user_id, login=profile.get("login"))
    except HTTPException:
        logger.info("github_connected_without_profile", user_id=user_id)

    return _back("connected")


@router.post("/disconnect")
async def disconnect(request: Request):
    """Forgets this user's stored token and profile."""
    user_id = _require_user_id(request)
    redis_client = request.app.state.redis
    await redis_client.delete(_token_key(user_id))
    await redis_client.delete(_profile_key(user_id))
    logger.info("github_disconnected", user_id=user_id)
    return {"connected": False}


# ─────────────────────────── repo ingestion ───────────────────────────


@router.get("/repos")
async def list_repos(
    request: Request,
    x_github_token: str | None = Header(default=None, alias="X-GitHub-Token"),
    search: str | None = Query(default=None, description="Optional case-insensitive name filter"),
):
    """Repositories the connected account can see, most-recently-updated first."""
    token = await _resolve_token(request, x_github_token)
    raw = await _github_get(
        "/user/repos",
        token,
        params={
            "per_page": 100,
            "sort": "updated",
            "affiliation": "owner,collaborator,organization_member",
        },
    )

    repos = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "full_name": r.get("full_name"),
            "default_branch": r.get("default_branch") or "main",
            "private": bool(r.get("private")),
            "clone_url": r.get("clone_url"),
            "description": r.get("description"),
            "updated_at": r.get("updated_at"),
            "owner": (r.get("owner") or {}).get("login"),
        }
        for r in (raw if isinstance(raw, list) else [])
    ]

    if search:
        needle = search.lower()
        repos = [r for r in repos if needle in (r["full_name"] or "").lower()]

    return {"repos": repos}


@router.get("/repos/{owner}/{repo}/branches")
async def list_branches(
    owner: str,
    repo: str,
    request: Request,
    x_github_token: str | None = Header(default=None, alias="X-GitHub-Token"),
):
    """Branches for one repository — populates the wizard's branch selector."""
    token = await _resolve_token(request, x_github_token)
    raw = await _github_get(f"/repos/{owner}/{repo}/branches", token, params={"per_page": 100})
    return {
        "branches": [
            {"name": b.get("name"), "commit_sha": (b.get("commit") or {}).get("sha")}
            for b in (raw if isinstance(raw, list) else [])
        ]
    }


@router.get("/repos/{owner}/{repo}/commits/{ref}")
async def get_head_commit(
    owner: str,
    repo: str,
    ref: str,
    request: Request,
    x_github_token: str | None = Header(default=None, alias="X-GitHub-Token"),
):
    """
    HEAD commit for a ref — gives a triggered rollout real git provenance
    (`commit_sha`/`commit_message` on the run) instead of an invented one.
    """
    token = await _resolve_token(request, x_github_token)
    raw = await _github_get(f"/repos/{owner}/{repo}/commits/{ref}", token)
    commit = raw.get("commit") or {}
    return {
        "sha": raw.get("sha"),
        "message": commit.get("message"),
        "author": (commit.get("author") or {}).get("name"),
        "committed_at": (commit.get("author") or {}).get("date"),
    }
