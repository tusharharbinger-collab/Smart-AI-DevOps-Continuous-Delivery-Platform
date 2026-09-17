"""
services/api-gateway/tests/test_webhook_registry.py

Covers webhook_registry.py's repo-URL parsing (pure) and its Redis-backed
register/resolve/unregister round trip (fake redis, no real connection) —
the mechanism a GitHub webhook delivery uses to resolve which tenant/
project an incoming repo belongs to before any tenant-scoped DB session
can exist. See the module's own docstring for why this can't be an
RLS-scoped Postgres query instead.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import webhook_registry


class FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)


def test_parse_full_name_https_clone_url():
    assert webhook_registry.parse_full_name("https://github.com/octocat/hello-world.git") == "octocat/hello-world"


def test_parse_full_name_https_browser_url_no_git_suffix():
    assert webhook_registry.parse_full_name("https://github.com/octocat/hello-world") == "octocat/hello-world"


def test_parse_full_name_ssh_url():
    assert webhook_registry.parse_full_name("git@github.com:octocat/hello-world.git") == "octocat/hello-world"


def test_parse_full_name_non_github_url_returns_none():
    assert webhook_registry.parse_full_name("https://gitlab.com/octocat/hello-world.git") is None


def test_parse_full_name_none_or_empty_returns_none():
    assert webhook_registry.parse_full_name(None) is None
    assert webhook_registry.parse_full_name("") is None


def test_parse_full_name_malformed_url_returns_none():
    assert webhook_registry.parse_full_name("https://github.com/just-an-owner") is None


def test_register_then_resolve_round_trip():
    redis_client = FakeRedis()

    asyncio.run(
        webhook_registry.register(redis_client, "octocat/hello-world", "proj-1", "tenant-1", "main")
    )
    result = asyncio.run(webhook_registry.resolve(redis_client, "octocat/hello-world"))

    assert result == {"project_id": "proj-1", "tenant_id": "tenant-1", "branch": "main"}


def test_resolve_is_case_insensitive_on_repo_name():
    redis_client = FakeRedis()
    asyncio.run(webhook_registry.register(redis_client, "OctoCat/Hello-World", "proj-1", "tenant-1", "main"))

    result = asyncio.run(webhook_registry.resolve(redis_client, "octocat/hello-world"))

    assert result is not None
    assert result["project_id"] == "proj-1"


def test_resolve_returns_none_for_unknown_repo():
    redis_client = FakeRedis()
    assert asyncio.run(webhook_registry.resolve(redis_client, "nobody/nothing")) is None


def test_unregister_removes_the_mapping():
    redis_client = FakeRedis()
    asyncio.run(webhook_registry.register(redis_client, "octocat/hello-world", "proj-1", "tenant-1", "main"))

    asyncio.run(webhook_registry.unregister(redis_client, "octocat/hello-world"))

    assert asyncio.run(webhook_registry.resolve(redis_client, "octocat/hello-world")) is None


def test_resolve_returns_none_for_corrupt_json():
    redis_client = FakeRedis()
    asyncio.run(redis_client.set(webhook_registry._key("octocat/hello-world"), "not-json"))

    assert asyncio.run(webhook_registry.resolve(redis_client, "octocat/hello-world")) is None
