"""Cache keys and backends (SPEC-AIP-002 §3.6, TODO-4, AC-6)."""

from __future__ import annotations

import os

import pytest

from navigator_orchestrator.engine.cache import InMemoryCache, RedisCache, cache_key
from navigator_orchestrator.engine.policy import Policy

POLICY = Policy(model="fake:echo")


def test_key_is_stable_across_dict_ordering() -> None:
    """Otherwise an identical request would miss the cache on a whim."""
    a = cache_key("echo", {"text": "ping", "n": 1}, POLICY)
    b = cache_key("echo", {"n": 1, "text": "ping"}, POLICY)
    assert a == b


def test_key_varies_with_workflow_input_and_policy() -> None:
    base = cache_key("echo", {"text": "ping"}, POLICY)
    assert base != cache_key("other", {"text": "ping"}, POLICY)
    assert base != cache_key("echo", {"text": "pong"}, POLICY)
    assert base != cache_key("echo", {"text": "ping"}, Policy(model="fake:echo-alt"))


async def test_in_memory_round_trip() -> None:
    cache = InMemoryCache()
    assert await cache.get("missing") is None
    await cache.set("k", {"text": "ping"})
    assert await cache.get("k") == {"text": "ping"}
    assert await cache.ping() is True


async def test_stored_values_are_copied_not_aliased() -> None:
    cache = InMemoryCache()
    payload = {"text": "ping"}
    await cache.set("k", payload)
    payload["text"] = "mutated"
    assert (await cache.get("k")) == {"text": "ping"}


async def test_expired_entries_are_evicted() -> None:
    cache = InMemoryCache()
    await cache.set("k", {"v": 1}, ttl_s=0)
    assert await cache.get("k") is None


async def test_clear() -> None:
    cache = InMemoryCache()
    await cache.set("k", {"v": 1})
    cache.clear()
    assert await cache.get("k") is None


@pytest.mark.skipif(
    not os.getenv("NAVIGATOR_REDIS_URL"),
    reason="no Redis reachable; CI runs this against a service container",
)
async def test_redis_backend_round_trips() -> None:  # pragma: no cover - CI only
    """The deployed backend, exercised against the real thing in CI."""
    cache = RedisCache(os.environ["NAVIGATOR_REDIS_URL"], namespace="navigator-orchestrator-test")
    key = cache_key("echo", {"text": "ping"}, POLICY)
    try:
        assert await cache.ping() is True
        await cache.set(key, {"text": "ping"}, ttl_s=30)
        assert await cache.get(key) == {"text": "ping"}
    finally:
        await cache.aclose()
