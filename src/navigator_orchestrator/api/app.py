"""App composition root (SPEC-AIP-002 §3.8, AC-4/AC-7).

This is the **only** place that builds clients, reads settings, or registers
workflows. Everything below it receives what it needs. Startup validates the
whole prompt registry so a missing or renamed prompt takes the app down at
boot instead of failing one request in production (AC-4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel

from navigator_orchestrator.api.authz import (
    HeaderPrincipalProvider,
    PrincipalProvider,
    StubPrincipalProvider,
)
from navigator_orchestrator.api.routes import router
from navigator_orchestrator.config import Settings, get_settings
from navigator_orchestrator.engine.cache import Cache, InMemoryCache, RedisCache
from navigator_orchestrator.engine.checkpoint import make_memory_checkpointer
from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import make_client
from navigator_orchestrator.engine.observability import (
    Observability,
    default_observability,
    langfuse_callbacks,
)
from navigator_orchestrator.engine.policy import Policy
from navigator_orchestrator.engine.prompts import PromptRegistry
from navigator_orchestrator.engine.runner import Runner
from navigator_orchestrator.engine.workflow import WorkflowRegistry
from navigator_orchestrator.store import (
    InMemoryRunLogStore,
    InMemoryRunStore,
    RunLogStore,
    RunStore,
)
from navigator_orchestrator.workflows.approval import ApprovalWorkflow
from navigator_orchestrator.workflows.echo import EchoWorkflow

__all__ = [
    "AppContext",
    "app",
    "build_app",
    "build_context",
    "create_app",
    "default_registry",
    "make_run_store",
]


def default_registry(checkpointer: Any | None = None) -> WorkflowRegistry:
    """Everything this deployment can run.

    `approval` needs the checkpointer: it pauses, and a pause with nowhere to
    persist is a lost run.
    """
    registry = WorkflowRegistry()
    registry.register(EchoWorkflow())
    registry.register(ApprovalWorkflow(checkpointer=checkpointer))
    return registry


def make_run_store(settings: Settings) -> RunStore | None:
    """Durable run records. In-process unless Postgres is configured."""
    if settings.run_store == "memory":
        return InMemoryRunStore()
    raise NotImplementedError(  # pragma: no cover - S8
        "the Postgres run store is not implemented yet; set NAVIGATOR_RUN_STORE=memory"
    )


def make_principal_provider(settings: Settings) -> PrincipalProvider:
    """Header-based identity when a principal is required, stub otherwise."""
    if settings.require_principal:
        return HeaderPrincipalProvider(settings.principal_header)
    return StubPrincipalProvider()


def make_cache(settings: Settings) -> Cache:
    """Redis when configured, in-process otherwise (dev and tests)."""
    if settings.redis_url:
        return RedisCache(settings.redis_url, namespace=settings.service_name)
    return InMemoryCache()


@dataclass(slots=True)
class AppContext:
    """Everything the routes need, built once per process."""

    settings: Settings
    prompts: PromptRegistry
    registry: WorkflowRegistry
    runner: Runner
    cache: Cache
    observability: Observability
    run_store: RunStore | None = None
    run_log_store: RunLogStore = field(default_factory=InMemoryRunLogStore)
    principal_provider: PrincipalProvider = field(default_factory=StubPrincipalProvider)
    warnings: list[str] = field(default_factory=list)


def build_context(
    settings: Settings,
    *,
    llm: BaseChatModel | None = None,
    client_factory: Callable[[Policy], BaseChatModel] | None = None,
    cache: Cache | None = None,
    observability: Observability | None = None,
    registry: WorkflowRegistry | None = None,
    run_store: RunStore | None = None,
    run_log_store: RunLogStore | None = None,
    checkpointer: Any | None = None,
) -> AppContext:
    """Wire the object graph.

    Overrides exist so tests inject a `FakeChatModel`. `client_factory` is the
    finer-grained hook: it sees each per-request `Policy`, which is what makes
    a `?model=` override rebuild the right client.
    """
    # `approval` pauses, so it needs somewhere to persist where it paused.
    saver = checkpointer if checkpointer is not None else make_memory_checkpointer()
    workflows = registry if registry is not None else default_registry(saver)
    resolved_run_store = run_store if run_store is not None else make_run_store(settings)
    resolved_run_log_store = run_log_store if run_log_store is not None else InMemoryRunLogStore()
    prompts = PromptRegistry.from_dir(settings.prompts_dir)
    # AC-4: fail fast, at boot, for every prompt any registered workflow may load.
    prompts.validate_all(workflows.prompt_refs())

    policy = settings.policy()
    resolved_cache = cache if cache is not None else make_cache(settings)
    obs = observability
    if obs is None:
        obs = default_observability(settings.service_name)
    obs.callbacks.extend(
        langfuse_callbacks(
            settings.langfuse_public_key,
            settings.langfuse_secret_key,
            settings.langfuse_host,
        )
    )

    # A per-request `?model=`/`?temperature=` override rebuilds the chat model
    # through this factory; an injected `llm` pins the same one for every policy.
    factory = client_factory
    if factory is None:
        factory = (lambda _policy: llm) if llm is not None else make_client

    deps = Deps(
        llm=llm if llm is not None else factory(policy),
        prompts=prompts,
        cache=resolved_cache,
        policy=policy,
    )
    runner = Runner(
        registry=workflows,
        deps=deps,
        observability=obs,
        cache=resolved_cache if settings.cache_enabled else None,
        default_policy=policy,
        client_factory=factory,
        run_store=resolved_run_store,
        run_log_store=resolved_run_log_store,
    )
    return AppContext(
        settings=settings,
        prompts=prompts,
        registry=workflows,
        runner=runner,
        cache=resolved_cache,
        observability=obs,
        run_store=resolved_run_store,
        run_log_store=resolved_run_log_store,
        principal_provider=make_principal_provider(settings),
    )


def build_app(
    settings: Settings | None = None,
    **overrides: Any,
) -> FastAPI:
    """Build the ASGI app. Context construction happens in the lifespan so a
    boot failure (e.g. a missing prompt) surfaces on startup, not on import."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved = settings if settings is not None else get_settings()
        application.state.context = build_context(resolved, **overrides)
        try:
            yield
        finally:
            cache = application.state.context.cache
            closer = getattr(cache, "aclose", None)
            if closer is not None:
                await closer()

    application = FastAPI(
        title="navigator-orchestrator-api",
        version="0.1.0",
        summary="Model-agnostic AI workflow engine (R0 spine)",
        lifespan=lifespan,
    )
    application.include_router(router)
    return application


def create_app() -> FastAPI:
    return build_app()


#: ASGI entrypoint — `uvicorn navigator_orchestrator.api.app:app`.
app = build_app()
