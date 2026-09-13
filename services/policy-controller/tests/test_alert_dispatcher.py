"""
services/policy-controller/tests/test_alert_dispatcher.py

Asserts alert payloads dispatch immediately (no batching/delay) upon the
three mandatory trigger conditions: autonomous rollback, a promotion halted
pending manual approval, and verification failing to reach a confident
verdict within the policy window. Also covers the no-webhook-configured
no-op path and that a webhook POST failure is logged, not raised (an alert
delivery failure must never crash the caller that triggered it).

`SLACK_WEBHOOK_URL` is read into a module-level constant at import time, so
tests monkeypatch `alert_dispatcher.SLACK_WEBHOOK_URL` directly rather than
the environment variable (which the module would no longer re-read).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import pytest

from src import alert_dispatcher


@pytest.fixture
def captured_webhook_posts(monkeypatch):
    calls = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, **kwargs):
            calls.append({"url": url, "json": json})
            return httpx.Response(200)

    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/webhook")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return calls


def test_rollback_alert_dispatches_immediately(captured_webhook_posts):
    asyncio.run(alert_dispatcher.alert_rollback("run-1", reason="SPRT rejected the canary"))

    assert len(captured_webhook_posts) == 1
    text = captured_webhook_posts[0]["json"]["text"]
    assert "ROLLBACK" in text
    assert "SPRT rejected the canary" in text
    assert "run-1" in text  # via the deep_link


def test_approval_required_alert_dispatches_immediately(captured_webhook_posts):
    asyncio.run(alert_dispatcher.alert_approval_required("run-2", "step_100_promotion", ["lead-sre", "platform-admin"]))

    assert len(captured_webhook_posts) == 1
    text = captured_webhook_posts[0]["json"]["text"]
    assert "BLOCKED" in text
    assert "step_100_promotion" in text
    assert "lead-sre" in text and "platform-admin" in text


def test_verification_timeout_alert_dispatches_immediately(captured_webhook_posts):
    asyncio.run(alert_dispatcher.alert_verification_timeout("run-3", samples_collected=42, samples_required=100))

    assert len(captured_webhook_posts) == 1
    text = captured_webhook_posts[0]["json"]["text"]
    assert "TIMEOUT" in text
    assert "42/100" in text


def test_alert_is_a_no_op_when_no_webhook_configured(monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    posted = {"called": False}

    class _ShouldNeverBeCalled:
        def __init__(self, *a, **k):
            posted["called"] = True

    monkeypatch.setattr(httpx, "AsyncClient", _ShouldNeverBeCalled)

    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "should not attempt delivery"))
    assert posted["called"] is False


def test_webhook_delivery_failure_is_logged_not_raised(monkeypatch):
    """An alerting outage must never take down the caller that triggered the alert."""
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/webhook")

    class _FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)

    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "webhook is down right now"))  # must not raise


def test_each_alert_type_gets_a_distinct_icon(captured_webhook_posts):
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "m"))
    asyncio.run(alert_dispatcher.send_alert("BLOCKED", "m"))
    asyncio.run(alert_dispatcher.send_alert("TIMEOUT", "m"))
    asyncio.run(alert_dispatcher.send_alert("SECURITY", "m"))

    texts = [c["json"]["text"] for c in captured_webhook_posts]
    icons_used = {t.split(" ", 1)[0] for t in texts}
    assert len(icons_used) == 4, f"expected 4 distinct icons for 4 distinct alert types, got {icons_used}"


def test_deep_link_included_when_provided(captured_webhook_posts):
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "message", deep_link="https://console/x"))
    assert "https://console/x" in captured_webhook_posts[0]["json"]["text"]


def test_no_deep_link_section_when_not_provided(captured_webhook_posts):
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "message"))
    assert "View details" not in captured_webhook_posts[0]["json"]["text"]


# --- Generic webhook channel (independent of Slack) ---


def test_generic_webhook_dispatches_alongside_slack(monkeypatch, captured_webhook_posts):
    """Two channels configured at once must both fire — one isn't exclusive of the other."""
    monkeypatch.setattr(alert_dispatcher, "GENERIC_WEBHOOK_URL", "https://pagerduty.test/hook")
    generic_calls = []

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kwargs):
            if url == "https://pagerduty.test/hook":
                generic_calls.append(json)
            else:
                captured_webhook_posts.append({"url": url, "json": json})
            return httpx.Response(200)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    asyncio.run(alert_dispatcher.alert_rollback("run-9", reason="both channels must fire"))

    assert len(captured_webhook_posts) == 1  # Slack
    assert len(generic_calls) == 1  # generic webhook
    assert generic_calls[0]["alert_type"] == "ROLLBACK"
    assert generic_calls[0]["message"] == "payments-service rollback: both channels must fire"


def test_generic_webhook_alone_works_without_slack_configured(monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "GENERIC_WEBHOOK_URL", "https://pagerduty.test/hook")
    calls = []

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kwargs):
            calls.append({"url": url, "json": json})
            return httpx.Response(200)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    asyncio.run(alert_dispatcher.send_alert("TIMEOUT", "no slack configured"))
    assert len(calls) == 1
    assert calls[0]["url"] == "https://pagerduty.test/hook"


def test_generic_webhook_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "GENERIC_WEBHOOK_URL", "https://pagerduty.test/hook")

    class _FailingAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "webhook down"))  # must not raise


# --- Email channel ---


def test_email_dispatched_when_smtp_configured(monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(alert_dispatcher, "ALERT_EMAIL_FROM", "alerts@platform.test")
    monkeypatch.setattr(alert_dispatcher, "ALERT_EMAIL_TO", "oncall@acme-corp.test,lead@acme-corp.test")

    sent = {}

    def fake_send_email_sync(alert_type, message, deep_link):
        sent["alert_type"] = alert_type
        sent["message"] = message
        sent["deep_link"] = deep_link

    monkeypatch.setattr(alert_dispatcher, "_send_email_sync", fake_send_email_sync)

    asyncio.run(alert_dispatcher.alert_rollback("run-10", reason="smtp channel test"))

    assert sent["alert_type"] == "ROLLBACK"
    assert "smtp channel test" in sent["message"]


def test_email_not_sent_when_smtp_not_fully_configured(monkeypatch):
    """Partial SMTP config (host but no recipient) must not attempt to send."""
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(alert_dispatcher, "ALERT_EMAIL_FROM", None)
    monkeypatch.setattr(alert_dispatcher, "ALERT_EMAIL_TO", None)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("must not attempt to send email without a full SMTP config")

    monkeypatch.setattr(alert_dispatcher, "_send_email_sync", _should_not_be_called)
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "incomplete smtp config"))  # must not raise


def test_email_send_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(alert_dispatcher, "ALERT_EMAIL_FROM", "alerts@platform.test")
    monkeypatch.setattr(alert_dispatcher, "ALERT_EMAIL_TO", "oncall@acme-corp.test")

    import smtplib

    def _raise(*args, **kwargs):
        raise smtplib.SMTPConnectError(421, "cannot connect")

    monkeypatch.setattr(alert_dispatcher, "_send_email_sync", _raise)
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "smtp is down"))  # must not raise


def test_no_channels_configured_is_a_clean_no_op(monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "GENERIC_WEBHOOK_URL", None)
    monkeypatch.setattr(alert_dispatcher, "SMTP_HOST", None)
    asyncio.run(alert_dispatcher.send_alert("ROLLBACK", "nothing configured"))  # must not raise
