"""
services/api-gateway/tests/test_run_risk_assessment.py

Covers get_run_risk_assessment (projects_router.py) — the new "AI Deployment
Risk Assessment" endpoint. This is the first real caller of the
predictive-risk scorer (predictive_risk_scorer.py, proxied unmodified via
score_project_predictive_risk) with an actual GitHub diff instead of nothing —
that scorer has existed since an earlier phase but was never exercised with
real data because no frontend code ever fetched a commit diff to feed it.

Same test convention as test_project_chatops.py / test_cost_history_endpoint.py:
call the router function directly with monkeypatched collaborators and a fake
DB, mocking only the network boundary (GitHub compare, explainability-service).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException

from src.routers import projects_router
from src.routers import github_router


class FakeRequest:
    def __init__(self, tenant_id="tenant-1"):
        self.state = type("S", (), {"tenant_id": tenant_id})()


class FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeDB:
    def __init__(self, prev_commit_sha=None):
        self._prev_commit_sha = prev_commit_sha

    async def execute(self, stmt, params=None):
        row = {"commit_sha": self._prev_commit_sha} if self._prev_commit_sha is not None else None
        return FakeResult(row)


class _FakeHttpResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.last_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        self.last_json = json
        if self._exc:
            raise self._exc
        return self._response


def _patch_common(monkeypatch, *, repo_url="https://github.com/acme/widget.git", run_overrides=None, tenant_id="tenant-1"):
    async def fake_load_project(db, project_id, tid, request=None):
        assert tid == tenant_id
        return {"project_id": project_id, "repo_url": repo_url, "name": "widget"}

    run = {
        "pipeline_run_id": "run-1",
        "commit_sha": "headsha1234567",
        "commit_message": "Add payment retry logic",
        "started_at": datetime(2026, 9, 29, tzinfo=timezone.utc),
    }
    if run_overrides:
        run.update(run_overrides)

    async def fake_assert_run(db, run_id, project_id, tid):
        assert tid == tenant_id
        return run

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_assert_run_belongs_to_project", fake_assert_run)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: tenant_id)
    return run


def test_not_available_when_project_has_no_connected_repo(monkeypatch):
    _patch_common(monkeypatch, repo_url=None)
    result = asyncio.run(
        projects_router.get_run_risk_assessment("proj-1", "run-1", FakeRequest(), db=FakeDB())
    )
    assert result == {
        "available": False,
        "reason": "no_repo",
        "message": "This project has no connected GitHub repository, so there's no commit diff to assess.",
    }


def test_not_available_when_run_has_no_recorded_commit(monkeypatch):
    _patch_common(monkeypatch, run_overrides={"commit_sha": None})
    result = asyncio.run(
        projects_router.get_run_risk_assessment("proj-1", "run-1", FakeRequest(), db=FakeDB())
    )
    assert result["available"] is False
    assert result["reason"] == "no_commit_recorded"


def test_not_available_for_the_earliest_tracked_deploy(monkeypatch):
    _patch_common(monkeypatch)
    result = asyncio.run(
        projects_router.get_run_risk_assessment("proj-1", "run-1", FakeRequest(), db=FakeDB(prev_commit_sha=None))
    )
    assert result["available"] is False
    assert result["reason"] == "first_tracked_deploy"


def test_not_available_when_redeploying_the_same_commit(monkeypatch):
    _patch_common(monkeypatch, run_overrides={"commit_sha": "samesha"})
    result = asyncio.run(
        projects_router.get_run_risk_assessment(
            "proj-1", "run-1", FakeRequest(), db=FakeDB(prev_commit_sha="samesha")
        )
    )
    assert result["available"] is False
    assert result["reason"] == "same_commit"


def test_404s_for_a_run_outside_the_project(monkeypatch):
    async def fake_load_project(db, project_id, tid, request=None):
        return {"project_id": project_id, "repo_url": "https://github.com/acme/widget.git"}

    async def fake_assert_run_not_found(db, run_id, project_id, tid):
        raise HTTPException(status_code=404, detail="Run not found for this project")

    monkeypatch.setattr(projects_router, "_load_project", fake_load_project)
    monkeypatch.setattr(projects_router, "_assert_run_belongs_to_project", fake_assert_run_not_found)
    monkeypatch.setattr(projects_router, "_get_tenant_id", lambda request: "tenant-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            projects_router.get_run_risk_assessment("proj-1", "someone-elses-run", FakeRequest(), db=FakeDB())
        )
    assert exc_info.value.status_code == 404


def test_happy_path_fetches_real_diff_and_forwards_it_to_the_unmodified_scorer(monkeypatch):
    _patch_common(monkeypatch)

    async def fake_compare_commits(request, owner, repo, base, head):
        assert (owner, repo) == ("acme", "widget")
        assert base == "prevsha0000000"
        assert head == "headsha1234567"
        return {
            "commits": [{"sha": "headsha1234567", "short_sha": "headsha", "message": "Add payment retry logic", "author": "Jane", "date": "2026-09-29T00:00:00Z", "url": "https://github.com/acme/widget/commit/headsha1234567"}],
            "files_changed": ["src/payments/retry.py"],
            "diff_text": "--- src/payments/retry.py\n+++ retry logic",
            "stats": {"additions": 40, "deletions": 2, "changed_files": 1},
            "ahead_by": 1,
            "compare_url": "https://github.com/acme/widget/compare/prevsha0000000...headsha1234567",
        }

    monkeypatch.setattr(github_router, "compare_commits", fake_compare_commits)

    fake_risk_payload = {
        "risk_score": 62,
        "risk_level": "MEDIUM",
        "summary": "Payment retry logic touched — moderate risk.",
        "risk_factors": [{"category": "Payments", "description": "Payment retry logic modified", "severity": "MEDIUM"}],
        "recommended_canary_steps": [{"weight": 10, "min_duration_seconds": 120}],
        "prescriptive_pre_deploy_checks": ["Verify idempotency of retried payment calls."],
    }
    fake_client = _FakeAsyncClient(_FakeHttpResponse(200, fake_risk_payload))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        projects_router.get_run_risk_assessment(
            "proj-1", "run-1", FakeRequest(), db=FakeDB(prev_commit_sha="prevsha0000000")
        )
    )

    assert result["available"] is True
    assert result["base_sha"] == "prevsha0000000"
    assert result["head_sha"] == "headsha1234567"
    assert result["stats"] == {"additions": 40, "deletions": 2, "changed_files": 1}
    assert result["commits"][0]["message"] == "Add payment retry logic"
    assert result["risk"] == fake_risk_payload
    # The real diff text and files_changed from GitHub were forwarded verbatim to the scorer -
    # never re-derived or fabricated by this endpoint.
    assert fake_client.last_json == {
        "commit_diff": "--- src/payments/retry.py\n+++ retry logic",
        "commit_message": "Add payment retry logic",
        "files_changed": ["src/payments/retry.py"],
    }


def test_returns_a_soft_unavailable_when_github_rejects_the_compare(monkeypatch):
    _patch_common(monkeypatch)

    async def fake_compare_commits_404(request, owner, repo, base, head):
        raise HTTPException(status_code=404, detail="Not found on GitHub (or the token cannot see it).")

    monkeypatch.setattr(github_router, "compare_commits", fake_compare_commits_404)

    result = asyncio.run(
        projects_router.get_run_risk_assessment(
            "proj-1", "run-1", FakeRequest(), db=FakeDB(prev_commit_sha="prevsha0000000")
        )
    )
    assert result["available"] is False
    assert result["reason"] == "github_error"


def test_returns_a_soft_unavailable_when_the_scorer_is_unreachable(monkeypatch):
    _patch_common(monkeypatch)

    async def fake_compare_commits(request, owner, repo, base, head):
        return {
            "commits": [],
            "files_changed": ["src/x.py"],
            "diff_text": "diff",
            "stats": {"additions": 1, "deletions": 0, "changed_files": 1},
            "ahead_by": 1,
            "compare_url": "https://github.com/acme/widget/compare/a...b",
        }

    monkeypatch.setattr(github_router, "compare_commits", fake_compare_commits)
    fake_client = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        projects_router.get_run_risk_assessment(
            "proj-1", "run-1", FakeRequest(), db=FakeDB(prev_commit_sha="prevsha0000000")
        )
    )
    assert result["available"] is False
    assert result["reason"] == "scorer_unavailable"
