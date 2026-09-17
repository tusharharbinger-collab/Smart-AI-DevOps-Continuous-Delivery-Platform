"""
services/pipeline-worker/tests/test_live_url_check.py

Guaranteed Live Web App CI/CD — real gap this closes: the platform could
report a project's live_url without ever actually requesting it (see
shared/live_url_check.py's module docstring). Covers verify_live_url's
retry/backoff behavior and its success/failure classification. No real
HTTP calls — `requests.get` is monkeypatched.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import requests

from shared import live_url_check
from shared.live_url_builder import build_live_url


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_verified_on_first_attempt_with_200(monkeypatch):
    calls = []
    monkeypatch.setattr(live_url_check.requests, "get", lambda url, timeout, allow_redirects: calls.append(url) or _FakeResponse(200))
    monkeypatch.setattr(live_url_check.time, "sleep", lambda s: None)

    result = live_url_check.verify_live_url("http://example.test/api/v1/widget")
    assert result == {"verified": True, "status_code": 200, "error": None}
    assert len(calls) == 1


def test_404_counts_as_verified(monkeypatch):
    # A 404 proves the ALB routed the request to a real, responding
    # application — this function's job is confirming SOMETHING real
    # answered, not judging whether the response is the right page.
    monkeypatch.setattr(live_url_check.requests, "get", lambda url, timeout, allow_redirects: _FakeResponse(404))
    result = live_url_check.verify_live_url("http://example.test/api/v1/widget")
    assert result["verified"] is True
    assert result["status_code"] == 404


def test_retries_on_5xx_then_succeeds(monkeypatch):
    responses = [_FakeResponse(503), _FakeResponse(503), _FakeResponse(200)]
    monkeypatch.setattr(live_url_check.requests, "get", lambda url, timeout, allow_redirects: responses.pop(0))
    sleep_calls = []
    monkeypatch.setattr(live_url_check.time, "sleep", lambda s: sleep_calls.append(s))

    result = live_url_check.verify_live_url("http://example.test/api/v1/widget", retries=5, delay_seconds=1)
    assert result["verified"] is True
    assert len(sleep_calls) == 2


def test_gives_up_after_retries_exhausted_on_persistent_5xx(monkeypatch):
    monkeypatch.setattr(live_url_check.requests, "get", lambda url, timeout, allow_redirects: _FakeResponse(500))
    monkeypatch.setattr(live_url_check.time, "sleep", lambda s: None)

    result = live_url_check.verify_live_url("http://example.test/api/v1/widget", retries=3)
    assert result["verified"] is False
    assert result["status_code"] == 500
    assert result["error"] == "HTTP 500"


def test_connection_failure_is_reported_as_the_error(monkeypatch):
    def raise_connection_error(url, timeout, allow_redirects):
        raise requests.ConnectionError("Name or service not known")

    monkeypatch.setattr(live_url_check.requests, "get", raise_connection_error)
    monkeypatch.setattr(live_url_check.time, "sleep", lambda s: None)

    result = live_url_check.verify_live_url("http://example.test/api/v1/widget", retries=2)
    assert result["verified"] is False
    assert "Name or service not known" in result["error"]


def test_no_url_is_reported_as_not_verified_without_making_a_request(monkeypatch):
    calls = []
    monkeypatch.setattr(live_url_check.requests, "get", lambda *a, **k: calls.append(1))

    result = live_url_check.verify_live_url(None)
    assert result["verified"] is False
    assert calls == []


# ─────────────── Guaranteed Live Web App CI/CD — build_live_url ───────────────
#
# Real gap found live (2026-09-17): every call site building this URL
# (api-gateway's displayed link, plus the URL this module's own
# verify_live_url actually checks in worker.py/aws_actuation_executor.py)
# concatenated base + path_prefix with no trailing slash — a browser only
# resolves a project's own relative asset references (<link href="style.css">)
# correctly when the current URL ends in "/". Confirmed live: a real
# onboarded app rendered completely unstyled with no working JS at exactly
# this link.


def test_build_live_url_adds_a_trailing_slash():
    assert build_live_url("smartcd-platform-alb.us-east-1.elb.amazonaws.com", "/api/v1/widget") == (
        "http://smartcd-platform-alb.us-east-1.elb.amazonaws.com/api/v1/widget/"
    )


def test_build_live_url_does_not_double_a_trailing_slash_already_present():
    assert build_live_url("alb.example.com", "/api/v1/widget/") == "http://alb.example.com/api/v1/widget/"


def test_build_live_url_is_none_without_a_base_url():
    assert build_live_url(None, "/api/v1/widget") is None


def test_build_live_url_is_none_without_a_path_prefix():
    assert build_live_url("alb.example.com", None) is None
    assert build_live_url("alb.example.com", "") is None
