"""
services/api-gateway/src/webhook_registry.py

Maps a connected GitHub repo ("owner/name") to the project/tenant it
belongs to, in Redis rather than Postgres. Real constraint this works
around: GitHub's webhook POST is an unauthenticated external request (no
platform JWT, no tenant context) — exactly the same shape as the OAuth
`/callback` redirect github_router.py already documents — but unlike that
callback, a webhook delivery needs to resolve WHICH tenant a repo belongs
to before any tenant-scoped DB session can be opened, and Postgres RLS on
`projects` would filter an unscoped SELECT to zero rows no matter which
non-superuser DSN ran it (see db/session.py's own docstring on why a
superuser bypass is never the right fix). Redis has no RLS, so this lookup
resolves tenant identity BEFORE a tenant-scoped session exists — mirroring
how auth_router.py's login resolves a user before tenant context exists,
just via Redis instead of an RLS-exempt table (a repo->tenant mapping
isn't sensitive the way password hashes are, so a dedicated exempt table
would be more machinery for no real benefit here).

Written at project-creation time (create_project) and removed at deletion
time (delete_project) — both best-effort, matching those endpoints'
existing "a missing cluster must not block the DB operation" convention.
Also re-written by the webhook-registration endpoint on every call, which
doubles as a self-healing resync if this Redis-only index is ever lost
(e.g. a Redis restart with no persistence) — the accepted tradeoff for
keeping this out of Postgres, same risk posture this codebase already
accepts for OAuth tokens and rollout_state.
"""
import json

import structlog

logger = structlog.get_logger(__name__)


def _key(repo_full_name: str) -> str:
    return f"webhook:repo:{repo_full_name.lower()}"


def parse_full_name(repo_url: str | None) -> str | None:
    """
    Extracts 'owner/repo' from a github.com URL in any form this platform
    accepts elsewhere (https clone URL with or without .git, the browser
    URL, or an ssh URL). Returns None for anything that isn't recognizably
    a github.com repo, rather than guessing — a wrong mapping is worse than
    no mapping.
    """
    if not repo_url:
        return None
    url = repo_url.strip()
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:") :]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        return None
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


async def register(redis_client, repo_full_name: str, project_id: str, tenant_id: str, branch: str) -> None:
    await redis_client.set(
        _key(repo_full_name),
        json.dumps({"project_id": project_id, "tenant_id": tenant_id, "branch": branch}),
    )
    logger.info("webhook_repo_mapping_registered", repo=repo_full_name, project_id=project_id)


async def unregister(redis_client, repo_full_name: str) -> None:
    await redis_client.delete(_key(repo_full_name))
    logger.info("webhook_repo_mapping_removed", repo=repo_full_name)


async def resolve(redis_client, repo_full_name: str) -> dict | None:
    raw = await redis_client.get(_key(repo_full_name))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("webhook_repo_mapping_corrupt", repo=repo_full_name)
        return None
