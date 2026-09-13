"""
services/policy-controller/src/alert_dispatcher.py

Alert Dispatcher: all 3 mandatory events per spec §12.5, across every
configured channel — Slack, a generic webhook (PagerDuty/Opsgenie/anything
that accepts a JSON POST), and email (SMTP). Each channel is independent:
one being unconfigured or failing never blocks or skips the others, and a
channel's own send failure is logged, never raised — an alerting outage
must not take down the caller (rollback/gate-block/timeout) that triggered
the alert.
"""
import asyncio
import os
import smtplib
from email.mime.text import MIMEText

import httpx
import structlog

logger = structlog.get_logger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
GENERIC_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO")  # comma-separated

_ICONS = {
    "ROLLBACK": "🚨",
    "BLOCKED": "🛑",
    "TIMEOUT": "⏱️",
    "SECURITY": "🛡️",
    # Phase 6 (§06-observability-platform-ops.md, 6.3): alerts about the
    # PLATFORM's own health (platform_health_monitor.py), distinct from the
    # above, which are about decisions made *about a verified service*.
    "PLATFORM_UNHEALTHY": "🔥",
    "PLATFORM_RECOVERED": "✅",
}


async def _dispatch_slack(alert_type: str, message: str, deep_link: str | None) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    text = f"{_ICONS.get(alert_type, '📢')} *{alert_type}*\n{message}"
    if deep_link:
        text += f"\n<{deep_link}|View details>"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(SLACK_WEBHOOK_URL, json={"text": text})
        except httpx.HTTPError as e:
            logger.error("slack_alert_dispatch_failed", error=str(e))


async def _dispatch_generic_webhook(alert_type: str, message: str, deep_link: str | None) -> None:
    if not GENERIC_WEBHOOK_URL:
        return
    payload = {"alert_type": alert_type, "message": message, "deep_link": deep_link}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(GENERIC_WEBHOOK_URL, json=payload)
        except httpx.HTTPError as e:
            logger.error("generic_webhook_alert_dispatch_failed", error=str(e))


def _send_email_sync(alert_type: str, message: str, deep_link: str | None) -> None:
    """Runs in a worker thread (see _dispatch_email) — smtplib has no async API."""
    body = message if not deep_link else f"{message}\n\nDetails: {deep_link}"
    email = MIMEText(body)
    email["Subject"] = f"[{alert_type}] Delivery pipeline alert"
    email["From"] = ALERT_EMAIL_FROM
    email["To"] = ALERT_EMAIL_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10.0) as server:
        server.starttls()
        if SMTP_USER and SMTP_PASSWORD:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(ALERT_EMAIL_FROM, ALERT_EMAIL_TO.split(","), email.as_string())


async def _dispatch_email(alert_type: str, message: str, deep_link: str | None) -> None:
    if not (SMTP_HOST and ALERT_EMAIL_FROM and ALERT_EMAIL_TO):
        return
    try:
        # smtplib is blocking I/O — never call it directly on the event
        # loop that's also processing verdicts/actuations.
        await asyncio.to_thread(_send_email_sync, alert_type, message, deep_link)
    except (smtplib.SMTPException, OSError) as e:
        logger.error("email_alert_dispatch_failed", error=str(e))


async def send_alert(alert_type: str, message: str, deep_link: str | None = None):
    channels = (
        _dispatch_slack(alert_type, message, deep_link),
        _dispatch_generic_webhook(alert_type, message, deep_link),
        _dispatch_email(alert_type, message, deep_link),
    )
    # Each channel already swallows its own errors (logged, not raised) —
    # gather() here is purely for concurrency, not error aggregation.
    await asyncio.gather(*channels)

    if not (SLACK_WEBHOOK_URL or GENERIC_WEBHOOK_URL or (SMTP_HOST and ALERT_EMAIL_FROM and ALERT_EMAIL_TO)):
        logger.info("alert_skipped_no_channel_configured", alert_type=alert_type, message=message)


async def alert_rollback(pipeline_run_id: str, reason: str):
    await send_alert(
        "ROLLBACK",
        f"payments-service rollback: {reason}",
        deep_link=f"https://console/pipelines/{pipeline_run_id}",
    )


async def alert_approval_required(pipeline_run_id: str, stage: str, roles: list[str]):
    await send_alert(
        "BLOCKED",
        f"Stage {stage} awaiting approval from: {', '.join(roles)}",
        deep_link=f"https://console/pipelines/{pipeline_run_id}/approve",
    )


async def alert_verification_timeout(pipeline_run_id: str, samples_collected: int, samples_required: int):
    await send_alert(
        "TIMEOUT",
        f"Verification indeterminate: {samples_collected}/{samples_required} samples collected",
        deep_link=f"https://console/pipelines/{pipeline_run_id}",
    )
