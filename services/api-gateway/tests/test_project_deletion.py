"""
services/api-gateway/tests/test_project_deletion.py

Real gap found live (2026-09-30): deleting a project used to only remove the DB row and the project's own
ECS/Kubernetes objects — an AI-provisioned draft's real extra resources (an S3 bucket, a database, …) and
the build's ECR image had no teardown path anywhere and outlived the project forever, still billing.
Covers the new async job-based `delete_project`/`get_delete_job_status` endpoints (the real step list a
project's own fields produce) and `_run_project_deletion_job`, the background worker that actually calls
pipeline-worker to tear everything down and only then removes the database records. Same
call-the-router-function-directly convention as test_infra_import_and_edit_endpoints.py — no TestClient,
real Postgres, AWS or Redis.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.routers import projects_router

PROJECT_ID = "22222222-2222-2222-2222-222222222222"
TENANT_ID = "tenant-1"
PIPELINE_ID = "33333333-3333-3333-3333-333333333333"
DRAFT_ID = "44444444-4444-4444-4444-444444444444"


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


class FakeRequest:
    def __init__(self, tenant_id=TENANT_ID, redis_client=None):
        self.state = type("S", (), {"tenant_id": tenant_id})()
        self.app = type("A", (), {"state": type("S2", (), {"redis": redis_client if redis_client is not None else FakeRedis()})()})()


class _Result:
    def __init__(self, row=None, rows=None):
        self._row, self._rows = row, rows if rows is not None else []

    def mappings(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return self._rows


def _project_row(**over):
    base = dict(
        project_id=PROJECT_ID, tenant_id=TENANT_ID, pipeline_id=PIPELINE_ID, name="testing-2",
        repo_url="https://github.com/tusharharbinger-collab/testing_2.git", branch="main", root_directory="./",
        dockerfile_path="Dockerfile", language=None, start_command=None, manifest_path=None, test_command=None,
        container_image="236087863083.dkr.ecr.us-east-1.amazonaws.com/testing-2", active_production_tag="v1.0.0",
        canary_tag="v1.1.0", status="HEALTHY", created_at=datetime.now(timezone.utc), path_prefix="/api/v1/testing-2",
        deploy_target="aws_ecs", deploy_mode="canary", live_url_status=None, live_url_verified_at=None,
    )
    base.update(over)
    return base


class FakeDB:
    """Backs `_load_project`'s SELECT and the infra_build_state lookup `delete_project` (the route handler)
    itself runs — the background job opens its own session, faked separately by `FakeAsyncSessionLocal`."""

    def __init__(self, project_row=None, infra_rows=None):
        self.project_row = project_row
        self.infra_rows = infra_rows or []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "SELECT project_id, tenant_id, pipeline_id" in sql:
            return _Result(row=self.project_row)
        if "FROM infra_build_state" in sql and "stack_name IS NOT NULL" in sql:
            return _Result(rows=self.infra_rows)
        return _Result()


def _disable_background_job(monkeypatch):
    """These tests only cover `delete_project`'s own step-list construction and job bookkeeping — the
    background job itself (`_run_project_deletion_job`) has its own dedicated tests below. Discarding the
    coroutine instead of scheduling it keeps `asyncio.run()` from ever seeing a "Task was destroyed but it
    is pending" warning for a task nothing here awaits."""
    def _fake_create_task(coro):
        coro.close()
        return None
    monkeypatch.setattr(projects_router.asyncio, "create_task", _fake_create_task)


def run(coro):
    return asyncio.run(coro)


# ─────────── delete_project: the real, per-project step list ───────────


def test_an_ecs_project_with_no_infra_draft_and_a_non_ecr_image_gets_only_cluster_webhook_and_records_steps(monkeypatch):
    _disable_background_job(monkeypatch)
    db = FakeDB(project_row=_project_row(container_image="registry.internal/testing-2"))

    result = run(projects_router.delete_project(PROJECT_ID, FakeRequest(), db=db))

    assert [s["key"] for s in result["steps"]] == ["cluster", "webhook", "records"]
    assert result["steps"][0]["label"] == "Stopping AWS ECS services and load balancer routing"
    assert "job_id" in result


def test_a_kubernetes_project_gets_the_kubernetes_labeled_cluster_step(monkeypatch):
    _disable_background_job(monkeypatch)
    db = FakeDB(project_row=_project_row(deploy_target="kubernetes", container_image="registry.internal/testing-2"))

    result = run(projects_router.delete_project(PROJECT_ID, FakeRequest(), db=db))

    cluster_step = next(s for s in result["steps"] if s["key"] == "cluster")
    assert cluster_step["label"] == "Removing Kubernetes Deployments, Service and HTTPRoute"


def test_an_adopted_pipeline_with_neither_repo_nor_image_has_no_cluster_step(monkeypatch):
    # A pipeline adopted from before projects existed (migration 0007) genuinely has nothing in a cluster.
    _disable_background_job(monkeypatch)
    db = FakeDB(project_row=_project_row(repo_url=None, container_image=None))

    result = run(projects_router.delete_project(PROJECT_ID, FakeRequest(), db=db))

    assert [s["key"] for s in result["steps"]] == ["webhook", "records"]


def test_an_ecr_backed_project_gets_an_ecr_deletion_step(monkeypatch):
    _disable_background_job(monkeypatch)
    db = FakeDB(project_row=_project_row())  # container_image already a real ECR URI in the default row

    result = run(projects_router.delete_project(PROJECT_ID, FakeRequest(), db=db))

    assert [s["key"] for s in result["steps"]] == ["cluster", "ecr", "webhook", "records"]


def test_a_project_with_a_linked_real_infra_draft_gets_a_stack_deletion_step(monkeypatch):
    _disable_background_job(monkeypatch)
    db = FakeDB(
        project_row=_project_row(container_image="registry.internal/testing-2"),
        infra_rows=[{"draft_id": DRAFT_ID, "stack_name": "smartcd-infra-draft-1"}],
    )

    result = run(projects_router.delete_project(PROJECT_ID, FakeRequest(), db=db))

    assert [s["key"] for s in result["steps"]] == ["cluster", f"infra:{DRAFT_ID}", "webhook", "records"]
    infra_step = next(s for s in result["steps"] if s["key"] == f"infra:{DRAFT_ID}")
    assert infra_step["label"] == "Deleting AI-provisioned infrastructure (CloudFormation stack)"


def test_the_job_is_saved_to_redis_and_readable_via_get_delete_job_status(monkeypatch):
    _disable_background_job(monkeypatch)
    redis = FakeRedis()
    db = FakeDB(project_row=_project_row(container_image="registry.internal/testing-2"))

    async def _run():
        result = await projects_router.delete_project(PROJECT_ID, FakeRequest(redis_client=redis), db=db)
        return result, await projects_router.get_delete_job_status(
            PROJECT_ID, result["job_id"], FakeRequest(redis_client=redis)
        )

    result, status = run(_run())
    assert status["job_id"] == result["job_id"]
    assert status["status"] == "RUNNING"
    assert status["project_name"] == "testing-2"


def test_get_delete_job_status_404s_for_an_unknown_or_cross_tenant_job():
    redis = FakeRedis()
    with pytest.raises(HTTPException) as e:
        run(projects_router.get_delete_job_status(PROJECT_ID, "no-such-job", FakeRequest(redis_client=redis)))
    assert e.value.status_code == 404


# ─────────── _run_project_deletion_job: the real teardown sequence ───────────


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        return self._payload


class _Client:
    """Routes by URL substring; records every call — same convention as
    test_infra_import_and_edit_endpoints.py's own `_Client`."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        self.calls.append((url, json))
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected URL {url}")


class _FakeSession:
    def __init__(self, raise_on_pipeline_delete=False):
        self.executed: list[str] = []
        self.committed = False
        self._raise_on_pipeline_delete = raise_on_pipeline_delete

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.executed.append(str(stmt))
        return _Result()

    async def commit(self):
        self.committed = True

    def begin_nested(self):
        session = self

        class _Nested:
            async def __aenter__(self_nested):
                return session

            async def __aexit__(self_nested, exc_type, exc, tb):
                if session._raise_on_pipeline_delete:
                    raise IntegrityError("DELETE FROM pipelines", {}, Exception("fk violation"))
                return False

        return _Nested()


async def _drain(redis, job_id):
    """`delete_project` schedules `_run_project_deletion_job` via `asyncio.create_task` and returns
    immediately — this polls the same Redis job record the real frontend would poll, until the background
    task (running concurrently on this same event loop) reaches a terminal state, so the test can assert on
    the job's FINAL result rather than racing it."""
    for _ in range(200):
        job = await projects_router._load_delete_job(redis, job_id)
        if job["status"] != "RUNNING":
            return job
        await asyncio.sleep(0)
    raise AssertionError("background deletion job never reached a terminal state")


def test_full_teardown_happy_path_runs_every_step_in_order_then_deletes_the_records(monkeypatch):
    redis = FakeRedis()
    client = _Client({
        "/services/deprovision-aws": _Resp(200, {"status": "deprovisioned"}),
        "/infra-provisioning/delete-stack": _Resp(200, {"status": "delete_requested"}),
        "/services/delete-image": _Resp(200, {"status": "deleted"}),
    })
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    fake_session = _FakeSession()
    monkeypatch.setattr(projects_router, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(projects_router.webhook_registry, "unregister", _AsyncNoop())

    project = _project_row()
    infra_rows = [{"draft_id": DRAFT_ID, "stack_name": "smartcd-infra-draft-1"}]

    async def _scenario():
        db = FakeDB(project_row=project, infra_rows=infra_rows)
        result = await projects_router.delete_project(PROJECT_ID, FakeRequest(redis_client=redis), db=db)
        return await _drain(redis, result["job_id"])

    job = run(_scenario())

    assert job["status"] == "COMPLETED"
    assert [s["status"] for s in job["steps"]] == ["done"] * len(job["steps"])
    called_urls = [url for url, _ in client.calls]
    assert any("/services/deprovision-aws" in u for u in called_urls)
    assert any("/infra-provisioning/delete-stack" in u for u in called_urls)
    assert any("/services/delete-image" in u for u in called_urls)
    assert fake_session.committed is True
    assert any("DELETE FROM projects" in s for s in fake_session.executed)
    assert any("DELETE FROM pipelines" in s for s in fake_session.executed)


def test_a_failed_cluster_step_still_lets_every_later_step_run_and_marks_the_job_failed(monkeypatch):
    # Real intent: cost-stopping must not be all-or-nothing - a transient pipeline-worker failure on the
    # ECS step must never block the ECR image or the infra stack from still being torn down.
    redis = FakeRedis()
    client = _Client({
        "/services/deprovision-aws": _Resp(502, text="pipeline-worker exploded"),
        "/infra-provisioning/delete-stack": _Resp(200, {"status": "delete_requested"}),
        "/services/delete-image": _Resp(200, {"status": "deleted"}),
    })
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    fake_session = _FakeSession()
    monkeypatch.setattr(projects_router, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(projects_router.webhook_registry, "unregister", _AsyncNoop())

    project = _project_row()
    infra_rows = [{"draft_id": DRAFT_ID, "stack_name": "smartcd-infra-draft-1"}]

    async def _scenario():
        db = FakeDB(project_row=project, infra_rows=infra_rows)
        result = await projects_router.delete_project(PROJECT_ID, FakeRequest(redis_client=redis), db=db)
        return await _drain(redis, result["job_id"])

    job = run(_scenario())

    assert job["status"] == "FAILED"
    by_key = {s["key"]: s for s in job["steps"]}
    assert by_key["cluster"]["status"] == "failed"
    assert by_key[f"infra:{DRAFT_ID}"]["status"] == "done"
    assert by_key["ecr"]["status"] == "done"
    assert by_key["records"]["status"] == "done"  # the DB rows are still removed even though AWS teardown partly failed


def test_a_pipeline_with_other_run_history_is_retained_and_reported_but_the_project_row_still_goes(monkeypatch):
    redis = FakeRedis()
    client = _Client({
        "/services/deprovision-aws": _Resp(200, {"status": "deprovisioned"}),
        "/services/delete-image": _Resp(200, {"status": "deleted"}),
    })
    monkeypatch.setattr(projects_router.httpx, "AsyncClient", client)
    fake_session = _FakeSession(raise_on_pipeline_delete=True)
    monkeypatch.setattr(projects_router, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(projects_router.webhook_registry, "unregister", _AsyncNoop())

    project = _project_row()

    async def _scenario():
        db = FakeDB(project_row=project, infra_rows=[])
        result = await projects_router.delete_project(PROJECT_ID, FakeRequest(redis_client=redis), db=db)
        return await _drain(redis, result["job_id"])

    job = run(_scenario())

    assert job["status"] == "COMPLETED"
    assert job["pipeline_retained"] is True
    assert fake_session.committed is True
    assert any("DELETE FROM projects" in s for s in fake_session.executed)


class _AsyncNoop:
    def __call__(self, *a, **kw):
        async def _inner():
            return None
        return _inner()
