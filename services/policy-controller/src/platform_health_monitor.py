"""
services/policy-controller/src/platform_health_monitor.py

Phase 6 (§06-observability-platform-ops.md, deliverable 6.3) — alerting on
the PLATFORM's own health, distinct from alert_dispatcher.py's existing
ROLLBACK/BLOCKED/TIMEOUT alerts (which are about decisions the platform
made about a *verified service*). Polls every backend service's /readyz
and fires a PLATFORM_UNHEALTHY alert through the same Slack webhook
(alert_dispatcher.send_alert) once a service has been unready for longer
than UNHEALTHY_ALERT_THRESHOLD_SECONDS — not on the first failed poll,
since a single missed health check during a deploy/restart isn't the
"the platform itself is broken" signal this exists to catch. Sends a
PLATFORM_RECOVERED alert once, the moment it comes back, so an operator
isn't left assuming the outage is still ongoing.

Honest scope note: this runs inside policy-controller, so it cannot detect
policy-controller's OWN total outage (a crashed process can't alert about
itself) — the `up{job="platform-services"}` panel on the Grafana dashboard
(6.2) is what actually covers that case, by design: Prometheus polls from
outside every one of these processes. Running this loop in the same
process it partly monitors is a real, accepted limitation, not an oversight
— a fully separate watchdog process would be genuinely more robust, but
the roadmap explicitly calls for reusing existing alert infrastructure
rather than standing up a second one for this phase.
"""
import asyncio
import os
import time

import httpx
import structlog

from src.alert_dispatcher import send_alert

logger = structlog.get_logger(__name__)

SERVICES = {
    "api-gateway": "http://api-gateway:8000/readyz",
    "pipeline-worker": "http://pipeline-worker:8001/readyz",
    "verification-engine": "http://verification-engine:8002/readyz",
    "policy-controller": "http://policy-controller:8003/readyz",
    "explainability-service": "http://explainability-service:8004/readyz",
}

POLL_INTERVAL_SECONDS = int(os.environ.get("PLATFORM_HEALTH_POLL_INTERVAL_SECONDS", "20"))
# 2 minutes by default — the SLA the roadmap's acceptance criteria names.
# Overridable so a live test can use a short threshold instead of waiting
# the full 2 minutes for real.
UNHEALTHY_ALERT_THRESHOLD_SECONDS = int(os.environ.get("PLATFORM_UNHEALTHY_ALERT_THRESHOLD_SECONDS", "120"))


async def _is_healthy(client: httpx.AsyncClient, url: str) -> bool:
    try:
        resp = await client.get(url, timeout=5.0)
        return resp.status_code == 200 and bool(resp.json().get("ready", False))
    except Exception:
        return False


async def platform_health_monitor_loop():
    first_unhealthy_at: dict[str, float] = {}
    already_alerted: dict[str, bool] = {}

    async with httpx.AsyncClient() as client:
        while True:
            try:
                for name, url in SERVICES.items():
                    healthy = await _is_healthy(client, url)
                    now = time.monotonic()

                    if healthy:
                        if already_alerted.get(name):
                            await send_alert("PLATFORM_RECOVERED", f"{name} is healthy again")
                            logger.info("platform_service_recovered", checked_service=name)
                        first_unhealthy_at.pop(name, None)
                        already_alerted[name] = False
                        continue

                    if name not in first_unhealthy_at:
                        first_unhealthy_at[name] = now
                        logger.warning("platform_service_unhealthy_detected", checked_service=name)

                    elapsed = now - first_unhealthy_at[name]
                    if elapsed >= UNHEALTHY_ALERT_THRESHOLD_SECONDS and not already_alerted.get(name):
                        await send_alert(
                            "PLATFORM_UNHEALTHY",
                            f"{name}'s /readyz has failed for over {int(elapsed)}s — the platform itself, "
                            "not a verified service's rollback, needs attention.",
                        )
                        already_alerted[name] = True
                        logger.error(
                            "platform_service_unhealthy_alert_fired", checked_service=name, elapsed_seconds=elapsed
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("platform_health_monitor_loop_failed", error=str(e))

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
