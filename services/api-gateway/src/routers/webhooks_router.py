"""
services/api-gateway/src/routers/webhooks_router.py

Phase 9.6 (P0 #1, 2026-09-16) — the real "git push -> cloud" trigger.
Real gap this closes: every rollout on this platform, including the ones
onboarded to AWS ECS with a real live_url, required a human to explicitly
trigger it (wizard click or a direct API call) — a plain `git push` to a
connected repo's tracked branch did nothing. This receiver is what makes a
push autonomous, reusing `_trigger_rollout_internal` (projects_router.py)
so a webhook-triggered run is byte-identical in every downstream way
(pipeline_executions row, Redis Streams publish, verification, OPA,
actuation) to one a human clicked "Deploy" for — nothing about the
existing chain had to change to support this.

Three real GitHub webhook-delivery constraints this is built against
(confirmed against GitHub's own docs, not assumed):
  1. Signature: `X-Hub-Signature-256` is `"sha256=" + hex(hmac_sha256(secret,
     raw_body))`; GitHub's own docs explicitly warn against a plain `==`
     comparison ("use a method like secure_compare... to mitigate timing
     attacks") — verified here with `hmac.compare_digest`.
  2. At-least-once delivery: GitHub does not auto-retry a delivery once its
     own retry attempts are exhausted, but the SAME delivery can legitimately
     arrive more than once. Deduped on `X-GitHub-Delivery` (a Redis SET NX,
     not a DB row — no tenant context exists yet at this point in the
     request, see webhook_registry.py) so a replayed delivery is a clean
     no-op rather than a second real rollout.
  3. 10-second ack budget: this handler never clones, builds, or runs
     verification inline — it does a signature check, a couple of Redis
     reads, and one INSERT + one Redis XADD (via _trigger_rollout_internal),
     the exact same "fast-ack, real work happens async via the existing
     pipeline-worker consumer group" shape every other trigger path here
     already uses. No new queueing hop was needed for that reason.

Deliberately unauthenticated (allowlisted in auth/middleware.py) for the
same structural reason github_router.py's OAuth `/callback` is: GitHub's
servers cannot send this platform's Authorization header. The HMAC
signature is the actual authentication here, not network trust.

Known, deliberate gap: no polling fallback yet (a periodic "does the
tracked branch's real HEAD match what we last deployed" reconciliation, as
a safety net for a webhook delivery GitHub itself failed to ever retry, or
this platform being unreachable when it tried). Tracked as a follow-up,
not silently dropped — see BACKLOG.md.
"""
import hashlib
import hmac
import json
import os
import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from shared import redis_streams as streams
from src.config import settings
from src.db.session import AsyncSessionLocal
from src.routers.github_router import _github_get
from src.routers.projects_router import STREAM_GATE1_CHECK, _load_project
from src import webhook_registry

# Webhook polling fallback (P0, 2026-09-16) — how often each connected
# repo's real branch HEAD is compared against the last commit this
# platform actually checked. GitHub does not auto-retry a dropped delivery
# indefinitely (see this module's own docstring) — this is the safety net.
# 15 min default: at ~5000 req/hr with a real GITHUB_TOKEN, this comfortably
# supports hundreds of connected repos without approaching GitHub's rate
# limit (one GET per repo per cycle).
WEBHOOK_POLL_INTERVAL_SECONDS = int(os.environ.get("WEBHOOK_POLL_INTERVAL_SECONDS", "900"))

router = APIRouter()
logger = structlog.get_logger(__name__)

# Comfortably longer than GitHub's own redelivery window, matching this
# codebase's existing convention for "long enough to catch a real replay,
# short enough not to accumulate forever" TTLs (e.g. clone_token's 900s,
# github OAuth token's 30-day one) — a push delivery replay is a
# same-day-or-never event in practice.
DELIVERY_DEDUP_TTL_SECONDS = 24 * 3600


def verify_signature(secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """
    GitHub's own docs: the header is always `sha256=<hex>`, and a plain
    `==` must never be used to compare it — timing-safe comparison only.
    """
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_push_event(payload: dict) -> dict | None:
    """
    Returns {full_name, branch, commit_sha, commit_message} for a genuine
    push-with-commits to a branch, or None for anything this platform has
    no real action for: a branch deletion, a tag push, or a payload with
    no resolvable commit — all of which must still be ACKed 200 by the
    caller (GitHub does not distinguish "not applicable" from "processed"
    at the HTTP layer), just without triggering anything.
    """
    if payload.get("deleted"):
        return None
    ref = payload.get("ref") or ""
    if not ref.startswith("refs/heads/"):
        return None
    branch = ref[len("refs/heads/") :]
    after = payload.get("after")
    if not after or after == "0" * 40:
        return None
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name")
    if not full_name:
        return None
    head_commit = payload.get("head_commit") or {}
    message = head_commit.get("message")
    if message is None:
        commits = payload.get("commits") or []
        if commits:
            message = commits[-1].get("message")
    return {"full_name": full_name, "branch": branch, "commit_sha": after, "commit_message": message}


async def _queue_gate1_check(
    redis_client,
    *,
    project_id: str,
    tenant_id: str,
    repo_url: str | None,
    branch: str,
    root_directory: str | None,
    dockerfile_path: str | None,
    language: str | None,
    manifest_path: str | None,
    start_command: str | None,
    test_command: str | None,
    commit_sha: str | None,
    commit_message: str | None,
) -> str:
    """
    The ONE place a gate1 check is ever queued — shared by the real-time
    webhook receiver and the polling fallback (poll_for_missed_webhook_deliveries)
    so the two trigger paths can never drift into queuing different-shaped
    payloads. Returns the new gate1_check_id.
    """
    gate1_check_id = str(uuid.uuid4())
    await streams.publish(
        redis_client,
        STREAM_GATE1_CHECK,
        {
            "gate1_check_id": gate1_check_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "repo_url": repo_url,
            "ref": branch,
            "root_directory": root_directory,
            "dockerfile_path": dockerfile_path,
            "language": language,
            "manifest_path": manifest_path,
            "start_command": start_command,
            "test_command": test_command,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
        },
    )
    return gate1_check_id


async def _mark_delivery_seen(redis_client, delivery_id: str) -> bool:
    """True if this is the first time this delivery id has been seen (caller
    should process it); False if it's a replay (caller should no-op)."""
    was_set = await redis_client.set(
        f"webhook:delivery:{delivery_id}", "1", nx=True, ex=DELIVERY_DEDUP_TTL_SECONDS
    )
    return bool(was_set)


@router.post("/github")
async def github_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    if not settings.GITHUB_WEBHOOK_SECRET:
        # Fail closed: a server with no secret configured has no way to
        # distinguish a real GitHub delivery from anyone else's POST, so it
        # must never silently accept — this is caught at registration time
        # too (register_project_webhook refuses the same way), but a
        # delivery could in principle arrive even if registration was never
        # called (a hand-configured webhook in GitHub's UI).
        logger.error("github_webhook_secret_not_configured")
        raise HTTPException(status_code=503, detail="Webhook receiving is not configured on this server.")

    if not verify_signature(settings.GITHUB_WEBHOOK_SECRET, raw_body, signature):
        logger.warning("github_webhook_signature_rejected", github_event=event, delivery_id=delivery_id)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if event == "ping":
        # GitHub's own "webhook was just created" self-test — must 200, not
        # be treated as an unhandled event type.
        return {"status": "pong"}
    if event != "push":
        return {"status": "ignored", "reason": f"event type '{event}' not handled"}

    if not delivery_id:
        # A real GitHub delivery always sets this; its absence past a valid
        # signature check would mean a same-secret request from something
        # that isn't actually GitHub. Fail closed rather than skip dedup.
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery header")

    redis_client = request.app.state.redis
    if not await _mark_delivery_seen(redis_client, delivery_id):
        logger.info("github_webhook_duplicate_delivery_ignored", delivery_id=delivery_id)
        return {"status": "duplicate_ignored"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed JSON payload")

    push = parse_push_event(payload)
    if push is None:
        return {"status": "ignored", "reason": "not a branch push with a resolvable commit"}

    mapping = await webhook_registry.resolve(redis_client, push["full_name"])
    if mapping is None:
        logger.info("github_webhook_unmapped_repo", repo=push["full_name"])
        return {"status": "ignored", "reason": "repo is not connected to any project on this platform"}

    tracked_branch = mapping.get("branch") or "main"
    if push["branch"] != tracked_branch:
        return {
            "status": "ignored",
            "reason": f"push to '{push['branch']}', tracked branch is '{tracked_branch}'",
        }

    tenant_id = mapping["tenant_id"]
    project_id = mapping["project_id"]

    try:
        async with AsyncSessionLocal() as db:
            async with db.begin():
                # Same parameterized SET LOCAL equivalent auth/middleware.py
                # uses for every authenticated request — this route has no
                # middleware-opened session (it's unauthenticated), so it
                # opens and scopes its own, same pattern, same connection
                # for the whole block.
                await db.execute(
                    text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id}
                )
                try:
                    project = await _load_project(db, project_id, tenant_id)
                except HTTPException:
                    logger.warning(
                        "github_webhook_project_missing", project_id=project_id, tenant_id=tenant_id
                    )
                    return {"status": "ignored", "reason": "mapped project no longer exists"}

        # Gate 1 (P0, 2026-09-16): queue a real build+test dry run instead
        # of triggering the rollout directly — pipeline-worker's gate1
        # consumer runs build_preview.py for real and only calls back into
        # POST /internal/{project_id}/gate1-result (passed=True) to
        # actually start a rollout once the new commit is proven to build
        # and pass its own tests. Kept OUTSIDE the DB transaction above
        # (that session is already closed by here) since this is a Redis
        # XADD, not a DB write — matches this handler's own fast-ack budget,
        # the real work (a genuine `docker build`) happens fully async.
        gate1_check_id = await _queue_gate1_check(
            redis_client,
            project_id=project_id,
            tenant_id=tenant_id,
            repo_url=project.get("repo_url"),
            branch=push["branch"],
            root_directory=project.get("root_directory"),
            dockerfile_path=project.get("dockerfile_path"),
            language=project.get("language"),
            manifest_path=project.get("manifest_path"),
            start_command=project.get("start_command"),
            test_command=project.get("test_command"),
            commit_sha=push["commit_sha"],
            commit_message=push["commit_message"],
        )
    except Exception:
        # ANY failure mid-processing — a deliberate HTTPException (no
        # linked pipeline, project vanished) or a genuinely unexpected one
        # (DB unreachable) — must not permanently swallow a legitimate
        # GitHub retry of THIS SAME delivery. Release the dedup claim so a
        # retry (GitHub's own short automatic window, or a human manually
        # clicking "Redeliver" in GitHub's webhook UI after fixing the
        # underlying issue) is actually reprocessed instead of silently
        # treated as a duplicate forever. The original status code/detail
        # still propagates unchanged — GitHub's own delivery log should
        # honestly show a failure, not a fake 200.
        await redis_client.delete(f"webhook:delivery:{delivery_id}")
        logger.error("github_webhook_processing_failed", delivery_id=delivery_id, exc_info=True)
        raise

    logger.info(
        "github_webhook_gate1_check_queued",
        project_id=project_id,
        gate1_check_id=gate1_check_id,
        commit_sha=push["commit_sha"],
    )
    return {"status": "gate1_check_queued", "gate1_check_id": gate1_check_id, "project_id": project_id}


# ─────────────────────────── polling fallback ───────────────────────────


async def _poll_one_repo(redis_client, repo_full_name: str, mapping: dict) -> None:
    """
    Compares one connected repo's real branch HEAD (GitHub API) against the
    last commit this platform actually checked. A mismatch means a push
    happened that no webhook delivery ever reached this platform for —
    queues the exact same gate1 check a real delivery would have, via the
    shared _queue_gate1_check helper (never a second, possibly-divergent
    trigger path).
    """
    project_id = mapping.get("project_id")
    tenant_id = mapping.get("tenant_id")
    branch = mapping.get("branch") or "main"
    if not project_id or not tenant_id or "/" not in repo_full_name:
        return
    owner, repo = repo_full_name.split("/", 1)

    token = settings.GITHUB_TOKEN.strip() or None
    try:
        commit_raw = await _github_get(f"/repos/{owner}/{repo}/commits/{branch}", token)
    except HTTPException as e:
        logger.warning("webhook_poll_github_fetch_failed", repo=repo_full_name, detail=e.detail)
        return
    except Exception as e:
        logger.warning("webhook_poll_github_fetch_failed", repo=repo_full_name, error=str(e))
        return

    real_head_sha = commit_raw.get("sha")
    if not real_head_sha:
        return

    # Reuses the SAME state the real-time path already maintains
    # (gate1_last_result:{project_id}.commit_sha) as the dedup signal —
    # deliberately not a second "last polled" key, so a commit checked via
    # a real webhook delivery is correctly never re-queued by polling too,
    # and vice versa.
    last_raw = await redis_client.get(f"gate1_last_result:{project_id}")
    last_checked_sha = None
    if last_raw:
        try:
            last_checked_sha = json.loads(last_raw).get("commit_sha")
        except json.JSONDecodeError:
            pass

    if last_checked_sha == real_head_sha:
        return

    logger.info(
        "webhook_poll_detected_unprocessed_commit",
        repo=repo_full_name,
        project_id=project_id,
        real_head_sha=real_head_sha,
        last_checked_sha=last_checked_sha,
    )

    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(text("SELECT set_config('app.active_tenant_id', :tid, true)"), {"tid": tenant_id})
            try:
                project = await _load_project(db, project_id, tenant_id)
            except HTTPException:
                logger.warning("webhook_poll_project_missing", project_id=project_id, tenant_id=tenant_id)
                return

    commit_message = (commit_raw.get("commit") or {}).get("message")
    await _queue_gate1_check(
        redis_client,
        project_id=project_id,
        tenant_id=tenant_id,
        repo_url=project.get("repo_url"),
        branch=branch,
        root_directory=project.get("root_directory"),
        dockerfile_path=project.get("dockerfile_path"),
        language=project.get("language"),
        manifest_path=project.get("manifest_path"),
        start_command=project.get("start_command"),
        test_command=project.get("test_command"),
        commit_sha=real_head_sha,
        commit_message=commit_message,
    )


async def poll_for_missed_webhook_deliveries(redis_client) -> None:
    """
    Webhook polling fallback (P0, 2026-09-16) — the safety net for a
    delivery GitHub itself never retried, or this platform being
    unreachable when it tried (GitHub does NOT auto-retry a failed
    delivery indefinitely — see this module's top docstring). Wired into
    main.py's lifespan as a periodic background task, same shape as every
    other periodic loop in this codebase.

    Enumerates candidate repos via Redis (`webhook:repo:*`), never
    Postgres — the mapping already lives there for the identical RLS
    reason webhook_registry.py's own docstring explains: no tenant context
    exists to safely scope a cross-tenant Postgres query. This is a
    deliberate, narrow exception (a Redis SCAN over non-tenant-sensitive
    routing data), never a superuser Postgres bypass — each repo's actual
    project data is still read through a real tenant-scoped session
    inside _poll_one_repo.
    """
    checked = 0
    async for key in redis_client.scan_iter(match="webhook:repo:*"):
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError:
            continue
        repo_full_name = key[len("webhook:repo:") :]
        try:
            await _poll_one_repo(redis_client, repo_full_name, mapping)
            checked += 1
        except Exception as e:
            logger.error("webhook_poll_repo_check_failed", repo=repo_full_name, error=str(e))
    logger.info("webhook_poll_cycle_complete", repos_checked=checked)
