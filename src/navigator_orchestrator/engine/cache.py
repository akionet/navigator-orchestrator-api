"""Idempotent-response cache (SPEC-AIP-002 §3.6, AC-6).

Keyed by `sha256(workflow.name + normalized_input + policy)`. Only workflows
that declare themselves idempotent opt in — a cache hit must return the same
answer the model would have, so this is a per-workflow claim, not a global
switch.

Redis is the deployed backend; the in-memory backend keeps BDD hermetic and
gives the same assertion (`AC-6`: second run, zero model calls).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from navigator_orchestrator.engine.policy import Policy

__all__ = ["Cache", "InMemoryCache", "RedisCache", "cache_key"]


def cache_key(workflow: str, payload: Any, policy: Policy) -> str:
    """Stable across dict ordering — otherwise identical requests would miss."""
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    material = f"{workflow}\x00{normalized}\x00{policy.fingerprint()}"
    return hashlib.sha256(material.encode()).hexdigest()


class Cache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], ttl_s: int | None = None) -> None: ...

    async def ping(self) -> bool: ...


@dataclass
class InMemoryCache:
    """Process-local backend for tests and single-node dev."""

    _entries: dict[str, tuple[float | None, dict[str, Any]]] = field(default_factory=dict)

    async def get(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at <= time.monotonic():
            del self._entries[key]
            return None
        return dict(value)

    async def set(self, key: str, value: dict[str, Any], ttl_s: int | None = None) -> None:
        expires_at = None if ttl_s is None else time.monotonic() + ttl_s
        self._entries[key] = (expires_at, dict(value))

    async def ping(self) -> bool:
        return True

    def clear(self) -> None:
        self._entries.clear()


class RedisCache:
    """Redis backend. Imported lazily so `redis` is not needed to run tests."""

    def __init__(self, url: str, namespace: str = "navigator-orchestrator") -> None:
        self._url = url
        self._namespace = namespace
        self._client: Any | None = None

    def _redis(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis  # noqa: PLC0415 - lazy, infra-only

            self._client = Redis.from_url(self._url, decode_responses=True)
        return self._client

    def _scoped(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self._redis().get(self._scoped(key))
        if raw is None:
            return None
        decoded: dict[str, Any] = json.loads(raw)
        return decoded

    async def set(self, key: str, value: dict[str, Any], ttl_s: int | None = None) -> None:
        await self._redis().set(self._scoped(key), json.dumps(value), ex=ttl_s)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis().ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
