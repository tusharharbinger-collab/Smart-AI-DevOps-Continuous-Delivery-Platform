"""
services/api-gateway/src/websocket/event_stream.py

WebSocket endpoint for structured pipeline state events (stage transitions,
traffic weight changes) — spec §11.1. Distinct from the SSE log stream
(logs_router.py): this channel is structured JSON, not raw text lines, and
is what PipelineDashboard.tsx's usePipelineEvents hook consumes.
"""
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import structlog

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.websocket("/{pipeline_run_id}")
async def pipeline_event_stream(websocket: WebSocket, pipeline_run_id: str):
    await websocket.accept()
    redis_client = websocket.app.state.redis
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"events:{pipeline_run_id}")

    try:
        # Send current state immediately so the UI doesn't wait for the next event.
        cached = await redis_client.get(f"state:{pipeline_run_id}")
        if cached:
            await websocket.send_text(cached if isinstance(cached, str) else cached.decode())

        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"]
                await websocket.send_text(data if isinstance(data, str) else data.decode())
    except WebSocketDisconnect:
        logger.info("pipeline_event_stream_disconnected", pipeline_run_id=pipeline_run_id)
    finally:
        await pubsub.unsubscribe(f"events:{pipeline_run_id}")
        await pubsub.aclose()
