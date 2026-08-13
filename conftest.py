"""Shared fixtures for unit and BDD suites (SPEC-AIP-002 §3.14).

Everything here is hermetic: `FakeClient`, an in-memory cache and an in-memory
span exporter. No test in this repo may reach a model provider — the single
`@live` smoke is opted into with `LIVE_LLM=true` and skipped otherwise.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from navigator_orchestrator.api.app import build_app, build_context
from navigator_orchestrator.config import Settings
from navigator_orchestrator.engine.cache import InMemoryCache
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.observability import Observability
from navigator_orchestrator.engine.prompts import PromptRegistry

T = TypeVar("T")

REPO_ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = REPO_ROOT / "prompts"


@pytest.fixture
def run_async() -> Callable[[Awaitable[T]], T]:
    """Drive async code from sync (pytest-bdd) steps."""

    def _run(awaitable: Awaitable[T]) -> T:
        return asyncio.run(_await(awaitable))

    async def _await(awaitable: Awaitable[T]) -> T:
        return await awaitable

    return _run


@pytest.fixture
def prompts() -> PromptRegistry:
    return PromptRegistry.from_dir(PROMPTS_DIR)


@pytest.fixture
def settings() -> Settings:
    """Hermetic settings — `_env_file=None` so a developer `.env` cannot leak in."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        model="fake:echo",
        prompts_dir=PROMPTS_DIR,
        redis_url=None,
        database_url=None,
        cache_enabled=True,
    )


@pytest.fixture
def fake_llm() -> FakeChatModel:
    return FakeChatModel(model_name="fake:echo")


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def observability(exporter: InMemorySpanExporter) -> Observability:
    return Observability.for_tests([exporter])


@pytest.fixture
def context(
    settings: Settings,
    fake_llm: FakeChatModel,
    cache: InMemoryCache,
    observability: Observability,
) -> Any:
    return build_context(settings, llm=fake_llm, cache=cache, observability=observability)


@pytest.fixture
def runner(context: Any) -> Any:
    return context.runner


@pytest.fixture
def app(
    settings: Settings,
    fake_llm: FakeChatModel,
    cache: InMemoryCache,
    observability: Observability,
) -> FastAPI:
    return build_app(settings, llm=fake_llm, cache=cache, observability=observability)


@asynccontextmanager
async def running_app(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Start the lifespan (so AC-4's boot validation actually runs), then serve."""
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def parse_sse(body: str) -> list[dict[str, Any]]:
    """Minimal SSE parser — `event:`/`data:` pairs separated by blank lines."""
    events: list[dict[str, Any]] = []
    name: str | None = None
    for line in body.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            payload = json.loads(line.split(":", 1)[1].strip())
            events.append({"event": name, "data": payload})
        elif not line.strip():
            name = None
    return events


@pytest.fixture
def bdd_context() -> Iterator[dict[str, Any]]:
    """Scratch space shared between Given/When/Then steps."""
    state: dict[str, Any] = {}
    yield state
    state.clear()
