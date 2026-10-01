"""
services/api-gateway/tests/test_build_detection_code_evidence.py

Covers github_router.py's detect_build_config wiring of the Phase D deep code-evidence scan
(AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.1): real source content is fetched for a bounded file
set, scanned for real SDK call sites, folded into infra_signals, and the archetype is re-derived when a
new signal changes which golden path fits. Mocks only the GitHub network boundary
(_fetch_repo_tree_and_manifests / _fetch_repo_file_content), same convention as other github_router tests.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio

from src.routers import github_router


class FakeRequest:
    pass


def _fake_fetch_tree(*, requirements_txt_content=None, file_paths=None, procfile_content=None):
    async def fake(request, owner, repo, ref, x_github_token):
        return {
            "file_paths": file_paths or ["requirements.txt", "src/app.py", "src/db.py"],
            "package_json_content": None,
            "requirements_txt_content": requirements_txt_content,
            "procfile_content": procfile_content,
            "yaml_manifest_content": None,
            "yaml_manifest_path": None,
            "truncated": False,
            "token": "tok",
        }
    return fake


def test_code_evidence_upgrades_a_signal_the_manifest_missed_and_rederives_archetype(monkeypatch):
    monkeypatch.setattr(
        github_router, "_fetch_repo_tree_and_manifests",
        _fake_fetch_tree(requirements_txt_content=""),  # empty manifest - NO database dependency listed
    )

    async def fake_fetch_content(owner, repo, ref, path, token):
        return "engine = create_engine(DATABASE_URL)\n" if path == "src/db.py" else "print('hello')\n"

    monkeypatch.setattr(github_router, "_fetch_repo_file_content", fake_fetch_content)

    result = asyncio.run(github_router.detect_build_config("acme", "widget", FakeRequest(), ref="main", x_github_token=None))

    assert result["language"] == "python"
    assert result["infra_signals"]["needs_database"] is True
    assert result["infra_signals"]["database_hint"] == "src/db.py:1 — engine = create_engine(DATABASE_URL)"
    assert result["archetype"] == "web_service_with_database"
    assert result["infra_signals"]["code_evidence"] == [
        {"file_path": "src/db.py", "line_number": 1, "snippet": "engine = create_engine(DATABASE_URL)", "category": "database"}
    ]


def test_manifest_signal_stays_the_primary_hint_when_code_evidence_corroborates_it(monkeypatch):
    monkeypatch.setattr(
        github_router, "_fetch_repo_tree_and_manifests",
        _fake_fetch_tree(requirements_txt_content="psycopg2-binary==2.9.9\n"),  # manifest ALREADY flags database
    )

    async def fake_fetch_content(owner, repo, ref, path, token):
        return "engine = create_engine(DATABASE_URL)\n" if path == "src/db.py" else "print('hello')\n"

    monkeypatch.setattr(github_router, "_fetch_repo_file_content", fake_fetch_content)

    result = asyncio.run(github_router.detect_build_config("acme", "widget", FakeRequest(), ref="main", x_github_token=None))

    assert result["infra_signals"]["needs_database"] is True
    assert result["infra_signals"]["database_hint"] == "psycopg2-binary"  # unchanged - manifest hint is primary
    assert len(result["infra_signals"]["code_evidence"]) == 1  # still recorded as corroboration


def test_no_matching_source_files_means_no_code_scan_and_no_change(monkeypatch):
    monkeypatch.setattr(
        github_router, "_fetch_repo_tree_and_manifests",
        _fake_fetch_tree(requirements_txt_content="", file_paths=["requirements.txt", "README.md"]),
    )

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("should never fetch file content when there are no scannable source files")

    monkeypatch.setattr(github_router, "_fetch_repo_file_content", fail_if_called)

    result = asyncio.run(github_router.detect_build_config("acme", "widget", FakeRequest(), ref="main", x_github_token=None))

    assert result["infra_signals"]["needs_database"] is False
    assert result["infra_signals"]["code_evidence"] == []
