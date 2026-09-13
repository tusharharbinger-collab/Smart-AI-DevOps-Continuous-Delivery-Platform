"""
services/verification-engine/src/publisher.py

Publishes signed verification verdicts onto a Redis Stream for
policy-controller's consumer group to pick up. The payload is HMAC-SHA256
signed to guarantee integrity.

Phase 5 (§05-reliability-scale.md, deliverable 5.1): this used to be a bare
pub/sub `publish()` to a per-run channel (`verdicts:{pipeline_run_id}`),
which policy-controller's `psubscribe("verdicts:*")` picked up — pub/sub
delivers to EVERY subscriber, so 2+ policy-controller replicas would each
independently actuate the same verdict (e.g. patch the same HTTPRoute
twice, redundantly but also racily). A single shared stream with a
consumer group (see shared/redis_streams.py) guarantees exactly one
policy-controller replica processes each verdict.

Uses the sync `redis` client directly (not shared/redis_streams.py's async
helpers) — this is a single non-blocking XADD, called from a synchronous
context (`main.py`'s `/verify` handler holds `app.state.redis_sync`); the
async helpers exist for the BLOCKING consumer-side read loop, which is the
only place sync-vs-async actually matters (see that module's docstring).
"""
import json
import redis
import structlog
from src.verdict_signer import build_signed_payload

logger = structlog.get_logger(__name__)

STREAM_VERDICTS = "stream:verdicts"


def publish_verdict(redis_client: redis.Redis, pipeline_run_id: str, verdict, trace_id: str | None = None) -> None:
    """
    Builds the HMAC-SHA256 signed payload and XADDs it to stream:verdicts.
    `trace_id` (Phase 6, §06-observability-platform-ops.md) travels with the
    entry so policy-controller can bind the SAME id its logs use for this
    run, rather than each service's logs for one pipeline_run_id being
    correlatable only by eye.
    """
    payload = build_signed_payload(verdict)
    entry = {"pipeline_run_id": pipeline_run_id, "trace_id": trace_id, **payload}
    redis_client.xadd(STREAM_VERDICTS, {"payload": json.dumps(entry)})
    logger.info(
        "verdict_published",
        pipeline_run_id=pipeline_run_id,
        status=verdict.status,
        verdict_id=verdict.verdict_id,
        stream=STREAM_VERDICTS,
    )
