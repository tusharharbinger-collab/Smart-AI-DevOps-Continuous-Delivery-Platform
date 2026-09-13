"""
services/pipeline-worker/src/pipeline/execution_state.py

Execution state, resume-after-restart, and tenant concurrency isolation.
Spec §4.3.
"""
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
import json
import redis
import structlog

logger = structlog.get_logger(__name__)


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"          # mid-shift halt (assignment requirement §2b)
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class PipelineExecutionState:
    pipeline_run_id: str
    tenant_id: str
    service_name: str
    current_stage: str
    current_traffic_weight: int
    status: StageStatus
    last_updated: str
    # Full DAG execution order — lets the UI render the pipeline's stage
    # list (PipelineDAG.tsx) instead of only knowing the current one.
    stages: list[str] = field(default_factory=list)
    # Phase 5 (reliability & scale): lets reconciler.py re-fetch this run's
    # registered pipeline manifest (pipelines.policy_yaml) to actually
    # resume it — without this, a crashed worker's interrupted pipeline
    # could be found in Postgres but nothing would know WHAT to resume it as.
    pipeline_id: str | None = None


class ExecutionStateStore:
    """
    Persists execution state to BOTH Redis (fast, for live polling) and
    PostgreSQL (durable, source of truth for resume-after-restart).

    `db` is a `src.db.PipelineWorkerDB` instance (Phase 5) — before that
    module existed, this constructor accepted a generic, never-actually-
    wired `db_session` that no caller in the real app ever passed, so this
    class's Postgres path was silently dead code; `db` now really is
    connected in `main.py`'s lifespan.
    """
    def __init__(self, redis_client: redis.Redis, db=None):
        self.redis = redis_client
        self.db = db

    def _lock_key(self, tenant_id: str, service_name: str) -> str:
        return f"lock:pipeline:{tenant_id}:{service_name}"

    def acquire_tenant_lock(self, tenant_id: str, service_name: str, ttl_seconds: int = 3600) -> bool:
        """
        Prevents two pipelines for the SAME tenant+service running concurrently.
        Different tenants, or the same tenant on a different service, are unaffected —
        this is per-(tenant, service), not a global lock.
        """
        key = self._lock_key(tenant_id, service_name)
        acquired = self.redis.set(key, "locked", nx=True, ex=ttl_seconds)
        return bool(acquired)

    def release_tenant_lock(self, tenant_id: str, service_name: str):
        self.redis.delete(self._lock_key(tenant_id, service_name))

    def save_state_sync(self, state: PipelineExecutionState):
        state_dict = {
            "pipeline_run_id": state.pipeline_run_id,
            "tenant_id": state.tenant_id,
            "service_name": state.service_name,
            "current_stage": state.current_stage,
            "current_traffic_weight": state.current_traffic_weight,
            "status": state.status.value if isinstance(state.status, StageStatus) else str(state.status),
            "last_updated": state.last_updated,
            "stages": state.stages,
        }
        payload = json.dumps(state_dict)
        self.redis.set(f"state:{state.pipeline_run_id}", payload, ex=86400)
        # api-gateway's WebSocket (src/websocket/event_stream.py) sends this
        # `state:` key once at connection time, then just subscribes to
        # `events:{run_id}` for anything after that — so every stage
        # transition must also be published here, or the UI only ever shows
        # whatever snapshot existed at the moment it connected and never
        # updates again (e.g. gets stuck showing RUNNING after the pipeline
        # has already completed).
        self.redis.publish(f"events:{state.pipeline_run_id}", payload)

        # Durable path (Phase 5): `start_pipeline` (worker.py) runs in a
        # worker thread via `asyncio.to_thread`, not on the event loop the
        # DB pool was created on — `run_from_thread` schedules the write
        # back onto that loop and blocks this thread for the result (see
        # db.py's docstring for the cross-event-loop bug this avoids). A
        # transient DB error must never abort an in-progress pipeline stage
        # over a persistence hiccup — the Redis write above already
        # succeeded and is what live polling/the UI actually reads.
        if self.db:
            try:
                self.db.run_from_thread(self.db.save_execution_state(state))
            except Exception as e:
                logger.error("db_save_state_failed", error=str(e), pipeline_run_id=state.pipeline_run_id)

    async def get_interrupted_pipelines(self) -> list[PipelineExecutionState]:
        """Called by reconciler.py on worker startup. Finds pipelines that were RUNNING when a worker died."""
        if not self.db:
            return []
        rows = await self.db.get_interrupted_pipelines()
        return [
            PipelineExecutionState(
                pipeline_run_id=str(r["pipeline_run_id"]),
                tenant_id=str(r["tenant_id"]),
                pipeline_id=str(r["pipeline_id"]) if r["pipeline_id"] else None,
                service_name=r["service_name"],
                current_stage=r["current_stage"],
                current_traffic_weight=r["current_traffic_weight"],
                status=StageStatus(r["status"]),
                last_updated=str(r["last_updated"]),
            )
            for r in rows
        ]
