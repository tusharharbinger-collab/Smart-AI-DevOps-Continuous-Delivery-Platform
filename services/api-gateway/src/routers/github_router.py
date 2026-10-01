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
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _github_get(path: str, token: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.get(f"{GITHUB_API}{path}", headers=_github_headers(token), params=params)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")

    if resp.status_code == 401:
        # Real bug found live: this used to raise a bare HTTP 401 from
        # api-gateway itself — but 401 from OUR OWN API means exactly one
        # thing everywhere else in this codebase: "your platform login
        # session is invalid," and the frontend's api client (client.ts)
        # treats ANY 401 as that, force-logging the user out and bouncing
        # them to /login. This 401 has nothing to do with the user's
        # platform session — it means the platform's STORED GitHub OAuth
        # token is stale/revoked. Conflating the two meant a stale GitHub
        # token silently logged users out of the whole platform the moment
        # they opened the repo picker, with a "session expired" message
        # that had nothing to do with what actually happened. 502 (this
        # server's own upstream dependency failed) is the correct status —
        # never re-introduce a bare 401 here.
        raise HTTPException(
            status_code=502,
            detail="GitHub rejected the stored token. Reconnect your GitHub account (Disconnect, then Connect GitHub again).",
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


async def compare_commits(request: Request, owner: str, repo: str, base: str, head: str) -> dict:
    """
    Real commit list + per-file diff between two SHAs via GitHub's compare API — this is the ground truth
    both the "what changed in this deploy" changelog and the AI Pre-Flight Risk Assessment
    (projects_router.py's /runs/{run_id}/risk-assessment) build on, never a guessed/fabricated diff.

    Falls back to an anonymous (unauthenticated) request for a public repo when no user token is usable —
    same rejected-token detection `_fetch_repo_tree_and_manifests` already uses, so a stale stored GitHub
    token doesn't block this for a public repo either.
    """
    try:
        token = await _resolve_token(request, None)
        raw = await _github_get(f"/repos/{owner}/{repo}/compare/{base}...{head}", token)
    except HTTPException as e:
        stale_token = e.status_code == 502 and "rejected the stored token" in str(e.detail)
        if e.status_code not in (400, 401) and not stale_token:
            raise
        logger.info("github_compare_falling_back_to_anonymous", owner=owner, repo=repo, reason=e.detail)
        raw = await _github_get(f"/repos/{owner}/{repo}/compare/{base}...{head}", None)

    files = raw.get("files") or []
    commits_raw = raw.get("commits") or []
    commits = [
        {
            "sha": c.get("sha"),
            "short_sha": (c.get("sha") or "")[:7],
            "message": ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0],
            "author": ((c.get("commit") or {}).get("author") or {}).get("name")
            or (c.get("author") or {}).get("login"),
            "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            "url": c.get("html_url"),
        }
        for c in commits_raw
    ]
    # Real unified diff text (GitHub's own `patch` field per file), concatenated for the risk scorer —
    # never fabricated. GitHub omits `patch` for binary files and very large diffs, which `.get()` handles.
    diff_text = "\n".join(f"--- {f.get('filename')}\n{f['patch']}" for f in files if f.get("patch"))
    return {
        "commits": commits,
        "files_changed": [f.get("filename") for f in files if f.get("filename")],
        "diff_text": diff_text,
        "stats": {
            "additions": sum(f.get("additions", 0) for f in files),
            "deletions": sum(f.get("deletions", 0) for f in files),
            "changed_files": len(files),
        },
        "ahead_by": raw.get("ahead_by"),
        "compare_url": raw.get("html_url"),
    }


async def _fetch_repo_file_content(owner: str, repo: str, ref: str, path: str, token: str | None) -> str | None:
    """Fetches one file's real text content (base64-decoded) - shared by manifest fetching
    (_fetch_repo_tree_and_manifests) and the Phase D code-evidence scan (detect_build_config), so both go
    through the exact same GitHub call shape and failure handling."""
    try:
        contents_raw = await _github_get(f"/repos/{owner}/{repo}/contents/{path}", token, params={"ref": ref})
        import base64

        if contents_raw.get("encoding") == "base64":
            return base64.b64decode(contents_raw["content"]).decode("utf-8")
    except Exception as e:
        logger.warning("repo_file_fetch_failed", owner=owner, repo=repo, path=path, error=str(e))
    return None


async def _fetch_repo_tree_and_manifests(
    request: Request,
    owner: str,
    repo: str,
    ref: str,
    x_github_token: str | None,
) -> dict:
    from shared.repo_scanner import MANIFEST_FILENAMES

    try:
        token = await _resolve_token(request, x_github_token)
        tree_raw = await _github_get(f"/repos/{owner}/{repo}/git/trees/{ref}", token, params={"recursive": "1"})
    except HTTPException as e:
        # 400 = no GitHub account connected, 401 = bare rejection, 502 = the stored token was rejected (that case
        # deliberately maps to 502, see _github_get). All three mean "no usable user token" - a PUBLIC repo can still be
        # read anonymously, so fall back instead of failing detection for a stale token.
        stale_token = e.status_code == 502 and "rejected the stored token" in str(e.detail)
        if e.status_code not in (400, 401) and not stale_token:
            raise
        logger.info("github_tree_fetch_falling_back_to_anonymous", owner=owner, repo=repo, reason=e.detail)
        token = None
        tree_raw = await _github_get(f"/repos/{owner}/{repo}/git/trees/{ref}", None, params={"recursive": "1"})

    tree = tree_raw.get("tree") or []
    file_paths = [e["path"] for e in tree if e.get("type") == "blob"]

    async def _fetch_text_file(path: str) -> str | None:
        return await _fetch_repo_file_content(owner, repo, ref, path, token)

    yaml_manifest_content = None
    yaml_manifest_path: str | None = None
    yaml_candidates = [p for p in file_paths if p.rsplit("/", 1)[-1] in MANIFEST_FILENAMES and p.count("/") <= 2]
    if yaml_candidates:
        yaml_manifest_path = sorted(yaml_candidates, key=lambda p: p.count("/"))[0]
        yaml_manifest_content = await _fetch_text_file(yaml_manifest_path)

    package_json_content = None
    if any(p.rsplit("/", 1)[-1] == "package.json" for p in file_paths):
        pkg_path = next((p for p in file_paths if p.rsplit("/", 1)[-1] == "package.json" and p.count("/") <= 2), None)
        if pkg_path:
            content = await _fetch_text_file(pkg_path)
            if content is not None:
                import json

                try:
                    package_json_content = json.loads(content)
                except json.JSONDecodeError as e:
                    logger.warning("package_json_parse_failed", owner=owner, repo=repo, error=str(e))

    requirements_txt_content = None
    req_path = next((p for p in file_paths if p.rsplit("/", 1)[-1] == "requirements.txt" and p.count("/") <= 2), None)
    if req_path:
        requirements_txt_content = await _fetch_text_file(req_path)

    procfile_content = None
    procfile_path = next((p for p in file_paths if p.rsplit("/", 1)[-1] == "Procfile" and p.count("/") <= 2), None)
    if procfile_path:
        procfile_content = await _fetch_text_file(procfile_path)

    return {
        "file_paths": file_paths,
        "package_json_content": package_json_content,
        "requirements_txt_content": requirements_txt_content,
        "procfile_content": procfile_content,
        "yaml_manifest_content": yaml_manifest_content,
        "yaml_manifest_path": yaml_manifest_path,
        "truncated": bool(tree_raw.get("truncated")),
        # Threaded through so a caller (detect_build_config's Phase D code-evidence scan) can fetch
        # additional file content without re-resolving the token or re-fetching the tree.
        "token": token,
    }


@router.get("/repos/{owner}/{repo}/build-detection")
async def detect_build_config(
    owner: str,
    repo: str,
    request: Request,
    ref: str = Query(default="main"),
    x_github_token: str | None = Header(default=None, alias="X-GitHub-Token"),
):
    """
    Real onboarding-flow gap this closes: looks at the repo file tree and manifests
    first to propose build method, commands, and networking suggestions.
    """
    from shared.repo_scanner import (
        apply_code_evidence,
        detect_build_method,
        infer_scan_language,
        match_golden_path_archetype,
        scan_source_evidence,
        select_files_for_code_scan,
        suggest_networking_defaults,
    )

    fetch_res = await _fetch_repo_tree_and_manifests(request, owner, repo, ref, x_github_token)
    file_paths = fetch_res["file_paths"]

    detection = detect_build_method(
        file_paths,
        package_json_content=fetch_res["package_json_content"],
        yaml_manifest_content=fetch_res["yaml_manifest_content"],
        yaml_manifest_path=fetch_res["yaml_manifest_path"],
        requirements_txt_content=fetch_res["requirements_txt_content"],
        procfile_content=fetch_res.get("procfile_content"),
    )
    infra = detection.infra_signals

    # AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.1 (Phase D) - deep code-evidence scan. Manifest
    # dependency names alone (above) can't tell "boto3 is listed" apart from "boto3 is actually called" -
    # this reads a bounded, real set of source files for actual SDK call sites. Additive only: a signal
    # the manifest already found keeps its manifest-derived hint; a signal ONLY code evidence found gets
    # turned on for the first time, and the archetype is re-derived off the same real precedence rules
    # since a newly-discovered signal can change which golden path actually fits.
    if infra is not None:
        # Real bug found live testing this end-to-end: `detection.language` is only ever set on the
        # "synthesized" build path — it stays None whenever a Dockerfile exists (the MOST common real
        # case, checked first), which made the code scan silently never run for the overwhelming
        # majority of repos. infer_scan_language re-derives it from the same manifest-presence signal,
        # independent of which build method was actually chosen.
        scan_paths = select_files_for_code_scan(file_paths, infer_scan_language(file_paths))
        if scan_paths:
            contents: dict[str, str] = {}
            for path in scan_paths:
                content = await _fetch_repo_file_content(owner, repo, ref, path, fetch_res["token"])
                if content is not None:
                    contents[path] = content
            evidence = scan_source_evidence(contents)
            if evidence:
                apply_code_evidence(infra, evidence)
                detection.archetype = match_golden_path_archetype(detection, file_paths, fetch_res.get("procfile_content"))

    return {
        "method": detection.method,
        "dockerfile_path": detection.dockerfile_path,
        "language": detection.language,
        "framework": detection.framework,
        "manifest_path": detection.manifest_path,
        "start_command": detection.start_command,
        "test_command": detection.test_command,
        "test_config_found": detection.test_config_found,
        "confidence": detection.confidence,
        "issues": detection.issues,
        "deploy_config": detection.deploy_config,
        "archetype": detection.archetype,
        "truncated": fetch_res["truncated"],
        "infra_signals": {
            "needs_database": infra.needs_database,
            "database_hint": infra.database_hint,
            "needs_cache": infra.needs_cache,
            "cache_hint": infra.cache_hint,
            "needs_object_storage": infra.needs_object_storage,
            "storage_hint": infra.storage_hint,
            "is_static_site": infra.is_static_site,
            # Phase D — real, cited evidence from source content, shown so a human can verify WHY a
            # signal fired instead of taking a bare boolean on faith.
            "code_evidence": [
                {"file_path": e.file_path, "line_number": e.line_number, "snippet": e.snippet, "category": e.category}
                for e in infra.code_evidence
            ],
        } if infra else None,
        **suggest_networking_defaults(detection),
    }


@router.get("/repos/{owner}/{repo}/repo-report")
async def get_repo_report(
    owner: str,
    repo: str,
    request: Request,
    ref: str = Query(default="main"),
    x_github_token: str | None = Header(default=None, alias="X-GitHub-Token"),
):
    """
    ML-based repo health report, risk anomaly score (IsolationForest), hosting cost
    prediction, and executive summary narrative for pre-onboarding decision-making.
    """
    from dataclasses import asdict
    from shared.repo_scanner import detect_build_method
    from shared.repo_report import (
        extract_repo_features,
        score_repo_risk,
        predict_hosting_cost,
        build_narrative,
    )

    fetch_res = await _fetch_repo_tree_and_manifests(request, owner, repo, ref, x_github_token)
    file_paths = fetch_res["file_paths"]

    detection = detect_build_method(
        file_paths,
        package_json_content=fetch_res["package_json_content"],
        yaml_manifest_content=fetch_res["yaml_manifest_content"],
        yaml_manifest_path=fetch_res["yaml_manifest_path"],
    )

    features = extract_repo_features(
        file_paths,
        package_json_content=fetch_res["package_json_content"],
        requirements_txt_content=fetch_res["requirements_txt_content"],
        detection=detection,
    )

    risk = score_repo_risk(features)
    cost = predict_hosting_cost(features)
    narrative = await build_narrative(
        risk_level=risk["risk_level"],
        risk_flags=risk["risk_flags"],
        cost=cost,
        features=features,
        findings=risk["findings"],
        readiness_score=risk["readiness_score"],
    )

    return {
        "features": asdict(features),
        "risk": risk,
        "cost": cost,
        "narrative": narrative,
        "truncated": fetch_res["truncated"],
    }

