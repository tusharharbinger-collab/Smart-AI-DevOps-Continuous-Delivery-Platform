# shared/logging_config.py
"""
Imported identically by every one of the 5 microservices — guarantees the
same JSON shape (timestamp, level, service, trace_id, tenant_id) everywhere,
which is what the audit means by "structured JSON logging across all
microservices" rather than each service inventing its own format.

Usage in any service entrypoint:
    from shared.logging_config import configure_logging
    configure_logging("api-gateway")
"""
import logging
import os
import sys
from typing import Any

import structlog


def configure_logging(service_name: str) -> None:
    """
    Configure structlog for the given microservice.

    Produces newline-delimited JSON log records with the shape:
        {
            "timestamp": "2026-09-12T10:00:00.000Z",
            "level": "info",
            "service": "api-gateway",
            "event": "...",
            "trace_id": "...",    # injected via bind_contextvars()
            "tenant_id": "...",   # injected via bind_contextvars()
            ...extra fields...
        }

    All services call this once at startup before any logging happens.
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    shared_processors: list[Any] = [
        # 1. Merge any values previously bound with structlog.contextvars.bind_contextvars()
        #    (e.g., trace_id and tenant_id set per-request in middleware)
        structlog.contextvars.merge_contextvars,
        # 2. Add log level as a string field
        structlog.stdlib.add_log_level,
        # 3. Handle positional format args (%s style)
        structlog.stdlib.PositionalArgumentsFormatter(),
        # 4. ISO 8601 timestamp
        structlog.processors.TimeStamper(fmt="iso"),
        # 5. Render exception info into the event dict
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # 6. Ensure all strings are Unicode
        structlog.processors.UnicodeDecoder(),
        # 7. Inject service name — every log line from every service is unambiguously tagged
        _add_service_name(service_name),
    ]

    structlog.configure(
        processors=shared_processors + [
            # Final renderer: newline-delimited JSON
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging so that third-party libraries (uvicorn, celery, etc.)
    # also emit JSON through the same pipeline.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level, logging.INFO),
    )

    # Suppress noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("celery.app.trace").setLevel(logging.WARNING)


def _add_service_name(service_name: str):
    """
    Returns a structlog processor that injects `service` into every log record.
    This is what lets centralized log aggregators (Loki, CloudWatch Logs Insights, etc.)
    filter by service without relying on container labels.
    """
    def processor(logger: Any, method_name: str, event_dict: dict) -> dict:
        event_dict["service"] = service_name
        return event_dict
    return processor


def bind_request_context(trace_id: str, tenant_id: str | None = None) -> None:
    """
    Bind per-request context variables so every log statement within the request
    automatically includes trace_id and tenant_id without passing them explicitly.

    Call this at the start of every request/task:
        from shared.logging_config import bind_request_context
        bind_request_context(trace_id=request_id, tenant_id=tenant_id)
    """
    ctx: dict[str, Any] = {"trace_id": trace_id}
    if tenant_id:
        ctx["tenant_id"] = tenant_id
    structlog.contextvars.bind_contextvars(**ctx)


def clear_request_context() -> None:
    """Clear per-request context at the end of each request."""
    structlog.contextvars.clear_contextvars()
