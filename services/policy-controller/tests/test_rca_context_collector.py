"""
services/policy-controller/tests/test_rca_context_collector.py

Tests safe context gathering for RCA:
- GitHub URL parser
- Fail-soft guarantees (network/auth/Loki failures never raise)
- Successful collection merges context
"""
import asyncio
from unittest.mock import AsyncMock, patch
from src.rca_context_collector import (
    _parse_github_owner_repo,
    gather_rca_context,
)


def test_parse_github_owner_repo():
    assert _parse_github_owner_repo("https://github.com/my-org/my-repo") == ("my-org", "my-repo")
    assert _parse_github_owner_repo("https://github.com/my-org/my-repo.git") == ("my-org", "my-repo")
    assert _parse_github_owner_repo("git@github.com:my-org/my-repo.git") == ("my-org", "my-repo")
    assert _parse_github_owner_repo(None) is None
    assert _parse_github_owner_repo("https://gitlab.com/some/repo") is None


def test_gather_rca_context_empty_params():
    res = asyncio.run(gather_rca_context(None, None, None))
    assert res == {
        "service_name": None,
        "commit_sha": None,
        "commit_message": None,
        "commit_diff": None,
        "error_logs": [],
    }


def test_gather_rca_context_fail_soft_on_db_exception():
    fake_db = AsyncMock()
    fake_db.get_execution_context_for_rca.side_effect = Exception("DB connection dropped")

    res = asyncio.run(gather_rca_context(fake_db, "tenant-1", "run-1"))
    # Never raises, returns safe structure
    assert res["error_logs"] == []
    assert res["commit_diff"] is None


def test_gather_rca_context_success():
    fake_db = AsyncMock()
    fake_db.get_execution_context_for_rca.return_value = {
        "service_name": "checkout",
        "commit_sha": "abc1234",
        "commit_message": "Fix checkout bug",
        "repo_url": "https://github.com/acme/checkout",
        "deploy_target": "kubernetes",
    }

    with patch("src.rca_context_collector._fetch_github_diff", new_callable=AsyncMock) as mock_diff, \
         patch("src.rca_context_collector._fetch_loki_error_logs", new_callable=AsyncMock) as mock_logs:
        mock_diff.return_value = "--- a/app.py\n+++ b/app.py\n"
        mock_logs.return_value = ["[ERROR] Connection reset by peer"]

        res = asyncio.run(gather_rca_context(fake_db, "tenant-1", "run-1"))
        assert res["service_name"] == "checkout"
        assert res["commit_sha"] == "abc1234"
        assert res["commit_diff"] == "--- a/app.py\n+++ b/app.py\n"
        assert res["error_logs"] == ["[ERROR] Connection reset by peer"]
