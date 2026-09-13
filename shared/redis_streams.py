"""
shared/redis_streams.py

Phase 5 (§05-reliability-scale.md, deliverable 5.1) — Redis Streams +
consumer-group helpers, replacing bare Redis pub/sub as the trigger
mechanism between services. Pub/sub delivers every message to EVERY
subscriber; running 2 replicas of a service meant each one independently
processed the same message (documented as a known gap before this phase).
A Streams consumer group instead guarantees exactly one consumer in the
group is handed each entry, with an explicit ack step and a pending-entries
list for recovering work from a consumer that died mid-processing — the
building block both pipeline-worker's job queue and policy-controller's
verdict queue are built on.

All functions are async and take a `redis.asyncio.Redis` client — every
consumer here runs its read loop inside a FastAPI app's asyncio event loop
(the same one serving /healthz), and `XREADGROUP ... BLOCK` is a genuinely
blocking-for-up-to-N-seconds call: on a SYNC client that would freeze the
whole event loop for the duration of every poll. The async client's BLOCK
is handled via non-blocking socket I/O, so other requests keep being served
while a consumer is waiting for new work.
"""
import json

import structlog

logger = structlog.get_logger(__name__)


async def ensure_group(redis_client, stream: str, group: str) -> None:
    """
    Idempotent: creates the stream (if it doesn't exist yet, via `mkstream`)
    and the consumer group starting from the beginning of the stream. Safe
    to call on every service startup — a `BUSYGROUP` error just means
    another replica (or a previous run) already created it.
    """
    try:
        await redis_client.xgroup_create(name=stream, groupname=group, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def publish(redis_client, stream: str, payload: dict) -> str:
    """XADDs a single-field ('payload', JSON-encoded) entry. Returns the new entry's stream id."""
    return await redis_client.xadd(stream, {"payload": json.dumps(payload)})


async def read_one(redis_client, stream: str, group: str, consumer: str, block_ms: int = 5000):
    """
    Blocks up to `block_ms` for one NEW entry (`>` — never yet delivered to
    any consumer in this group). Returns (message_id, payload_dict), or
    None on timeout. The caller must call `ack()` only after the message
    has been fully and successfully processed — an un-acked message stays
    in the group's pending-entries list for `claim_stale_pending()` to
    recover if this consumer dies before acking.
    """
    resp = await redis_client.xreadgroup(group, consumer, {stream: ">"}, count=1, block=block_ms)
    if not resp:
        return None
    _, messages = resp[0]
    if not messages:
        return None
    message_id, fields = messages[0]
    return message_id, json.loads(fields["payload"])


async def ack(redis_client, stream: str, group: str, message_id: str) -> None:
    await redis_client.xack(stream, group, message_id)


async def claim_stale_pending(redis_client, stream: str, group: str, consumer: str, min_idle_ms: int):
    """
    Recovers entries that were delivered to some consumer (possibly this
    one, in an earlier crashed run) but never acked, and have now been
    pending for at least `min_idle_ms` — i.e. that consumer almost
    certainly died mid-processing. Reassigns them to `consumer` via
    XAUTOCLAIM and returns them the same shape as `read_one`, ready to be
    retried (or dead-lettered by the caller based on `get_delivery_count`).
    """
    _, claimed, _ = await redis_client.xautoclaim(stream, group, consumer, min_idle_time=min_idle_ms, start_id="0-0")
    return [(message_id, json.loads(fields["payload"])) for message_id, fields in claimed]


async def stream_length(redis_client, stream: str) -> int:
    """Total entries currently on the stream (Phase 6, §06-observability-platform-ops.md, 6.2 — queue depth)."""
    try:
        return await redis_client.xlen(stream)
    except Exception:
        # A stream with no entries yet (XGROUP CREATE with MKSTREAM not run,
        # or nothing published) doesn't exist as a key at all — 0 is the
        # correct depth, not an error worth propagating to a metrics scrape.
        return 0


async def pending_count(redis_client, stream: str, group: str) -> int:
    """
    Entries delivered to a consumer but not yet acked — a growing number
    here means consumers are falling behind or dying mid-processing
    (Phase 5's stale-pending reclaim loop is what recovers them).
    """
    try:
        summary = await redis_client.xpending(stream, group)
        return summary["pending"] if summary else 0
    except Exception:
        return 0


async def get_delivery_count(redis_client, stream: str, group: str, message_id: str) -> int:
    """
    How many times this entry has been delivered to a consumer (via the
    initial read + every subsequent claim). Callers use this to distinguish
    "just needs one more retry" from "poison message, give up and
    dead-letter it" — an entry that keeps crashing every consumer that
    picks it up should not retry forever.
    """
    entries = await redis_client.xpending_range(stream, group, min=message_id, max=message_id, count=1)
    if not entries:
        return 0
    return entries[0]["times_delivered"]
