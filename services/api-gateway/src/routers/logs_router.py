"""
services/api-gateway/src/routers/logs_router.py

SSE endpoint for execution logs — spec §11.1.

Polls the durable `logs:{pipeline_run_id}` Redis LIST (pipeline-worker's
`_log()`) rather than subscribing to a pub/sub channel. Real bug found
live: the original pub/sub design lost every line for any run a viewer
wasn't ALREADY subscribed to before it started — this platform's demo
pipeline completes in ~100ms, measurably faster than a client can open an
SSE connection, so pub/sub's fire-and-forget delivery meant the "Live
execution log" panel showed nothing for essentially every real click of
"Trigger New Rollout." A list is replayable: connecting after the run
already finished still shows its full log, and a still-running pipeline's
new lines keep streaming in as they're appended.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
import structlog

from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

POLL_INTERVAL_SECONDS = 0.5


TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ROLLED_BACK"}


@router.get("/{pipeline_run_id}/logs/stream")
async def stream_logs(pipeline_run_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    # Real bug found live (adversarial cross-tenant test): this endpoint used
    # to stream `logs:{run_id}` straight from Redis with no tenant check at
    # all — nothing stopped tenant B from watching tenant A's live execution
    # log just by knowing/guessing its run_id. `db` is the RLS-scoped
    # session, so a zero-row result means "not mine or doesn't exist."
    #
    # `status` (COALESCE'd the same way every other run-status read in this
    # codebase must be — see CLAUDE.md's own trap note) decides which
    # fallback path below applies: a run still RUNNING/PENDING keeps polling
    # Redis exactly as before even if it's momentarily empty (its first line
    # just hasn't landed yet); only a run that's already terminal AND has
    # nothing left in Redis falls back to `stage_logs`.
    owns_run = await db.execute(
        text(
            """
            SELECT COALESCE(es.status, pe.status) AS status
            FROM pipeline_executions pe
            LEFT JOIN execution_state es ON es.pipeline_run_id = pe.pipeline_run_id
            WHERE pe.pipeline_run_id = :run_id
            """
        ),
        {"run_id": pipeline_run_id},
    )
    run = owns_run.mappings().first()
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    redis_client = request.app.state.redis
    key = f"logs:{pipeline_run_id}"

    # Real gap found live (2026-09-18): `logs:{run_id}` carries a 24h Redis
    # TTL (pipeline-worker's `_log()`) and, until now, nothing durable
    # backed it — `stage_logs` had the right schema/RLS since Phase 8 but
    # nothing wrote to it. A viewer opening a run older than a day got an
    # empty Redis list forever, indistinguishable from a run that never
    # logged anything, and the generator below would just poll an
    # eternally-empty key rather than terminate.
    #
    # This fetch MUST happen here, before the response is constructed, not
    # inside `event_generator()` below — `db` is `request.state.db`, scoped
    # to the route handler's own transaction by the session middleware
    # (CLAUDE.md's own trap note); EventSourceResponse iterates the
    # generator AFTER this function returns, by which point that
    # transaction is already closed, and awaiting `db.execute(...)` from
    # inside the generator raised a real, confirmed-live
    # `InvalidRequestError: Can't operate on closed transaction`.
    historical_lines: list[str] | None = None
    if run["status"] in TERMINAL_STATUSES and not await redis_client.llen(key):
        historical = await db.execute(
            text("SELECT content FROM stage_logs WHERE run_id = :run_id ORDER BY created_at ASC"),
            {"run_id": pipeline_run_id},
        )
        # A real, non-None result here means "we already know this run is
        # terminal and Redis has nothing left" — so an empty list is still
        # authoritative (a terminal run that logged nothing, or one that
        # predates this fix) and must still close the stream rather than
        # fall through to polling a key that can never receive new data.
        historical_lines = historical.scalars().all()

    async def event_generator():
        if historical_lines is not None:
            for line in historical_lines:
                yield {"event": "log", "data": line}
            # Nothing further will ever be appended to a terminal run —
            # close the stream instead of polling forever for lines that
            # can never arrive.
            return

        next_index = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                lines = await redis_client.lrange(key, next_index, -1)
            except Exception as e:
                logger.error("log_poll_failed", pipeline_run_id=pipeline_run_id, error=str(e))
                lines = []
            for line in lines:
                yield {"event": "log", "data": line}
            next_index += len(lines)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return EventSourceResponse(event_generator())
