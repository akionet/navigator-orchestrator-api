"""Runner: validate → run graph → stream (SPEC-AIP-002 §3.5, AC-1/AC-5/AC-6).

The contract boundary lives here, not in the API layer: invalid input is
rejected *before* any node executes, and the output is validated *before* the
terminal `final` event. A node exception becomes a structured `error` event
rather than a broken stream.

LangGraph's `astream_events` shape is the one thing likely to drift across
versions, so all of the mapping is confined to `_map_event` — a version bump
touches one function.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langgraph.types import Command
from opentelemetry import trace
from opentelemetry.trace import Span

from navigator_orchestrator.engine.cache import Cache, cache_key
from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import make_client, text_of, usage_of
from navigator_orchestrator.engine.observability import Observability, Usage
from navigator_orchestrator.engine.policy import Policy
from navigator_orchestrator.engine.state import ContractError, validate_input, validate_output
from navigator_orchestrator.engine.workflow import Workflow, WorkflowRegistry
from navigator_orchestrator.events import (
    ErrorEvent,
    Event,
    FinalEvent,
    InterruptEvent,
    NodeEvent,
    TokenEvent,
)
from navigator_orchestrator.store import (
    Principal,
    RunLogStore,
    RunNotFoundError,
    RunState,
    RunStore,
)

__all__ = ["Runner"]


@dataclass(slots=True)
class Runner:
    """Executes registered workflows and turns them into an event stream.

    Token streaming needs no cooperation from a node: LangGraph reports the
    chat model's stream as `on_chat_model_stream`, and `_map_event` turns that
    into SSE. Nodes just call the model.
    """

    registry: WorkflowRegistry
    deps: Deps
    observability: Observability
    cache: Cache | None = None
    checkpointer: Any | None = None
    default_policy: Policy = field(default_factory=Policy)
    #: How a per-run `Policy` becomes a chat model. Injected so tests bind a
    #: `FakeChatModel` and so a per-request `?model=` override rebuilds one.
    client_factory: Callable[[Policy], BaseChatModel] = make_client
    #: Durable run + decision records. `None` keeps R0 behaviour — runs are
    #: ephemeral and nothing can pause.
    run_store: RunStore | None = None
    run_log_store: RunLogStore | None = None
    _clients: dict[str, BaseChatModel] = field(default_factory=dict)

    def client_for(self, policy: Policy) -> BaseChatModel:
        """Chat model for this policy, cached — clients are per-config, not per-run."""
        key = policy.fingerprint()
        if key not in self._clients:
            self._clients[key] = self.client_factory(policy)
        return self._clients[key]

    def run(
        self,
        name: str,
        raw_input: Any,
        policy: Policy | None = None,
        principal: Principal | None = None,
    ) -> AsyncIterator[Event]:
        """Validate eagerly, then stream.

        Deliberately **not** an `async def` generator: `UnknownWorkflowError`
        (404) and `ContractError` (422) must raise at call time so the API can
        answer with a status code instead of a half-open SSE stream.
        """
        workflow = self.registry.get(name)
        payload = validate_input(workflow.Input, raw_input)
        return self._stream(workflow, payload, policy or self.default_policy, principal=principal)

    def resume(
        self,
        name: str,
        run_id: str,
        decision: Mapping[str, Any],
        policy: Policy | None = None,
    ) -> AsyncIterator[Event]:
        """Continue a paused run with a decision (SPEC-AIP-003 AC-3).

        The caller here is deliberately *not* required to be the caller that
        started the run — separation of duties is the point. All that is needed
        is the run id.
        """
        workflow = self.registry.get(name)
        return self._stream(
            workflow,
            payload=None,
            policy=policy or self.default_policy,
            run_id=run_id,
            resume_with=decision,
        )

    async def _stream(  # noqa: PLR0915 - one lifecycle keeps emission/order explicit
        self,
        workflow: Workflow[Any, Any],
        payload: Any,
        policy: Policy,
        *,
        run_id: str | None = None,
        resume_with: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> AsyncIterator[Event]:
        resuming = resume_with is not None
        run_id = run_id or uuid4().hex
        usage = Usage()
        key = None if resuming else self._cache_key(workflow, payload, policy)
        node_spans: dict[str, Span] = {}

        attributes = {
            "workflow.name": workflow.name,
            "workflow.run_id": run_id,
            "policy.model": policy.model,
            "workflow.resumed": resuming,
        }
        with self.observability.span("workflow.run", **attributes) as run_span:
            try:
                if not resuming:
                    await self._open_run(run_id, workflow, policy, principal)
                cached = await self._lookup(key)
                if cached is not None:
                    run_span.set_attribute("cache.hit", True)
                    await self._mark(run_id, "completed")
                    final_event = FinalEvent(run_id=run_id, output=cached, cached=True)
                    await self._log(workflow.name, final_event)
                    yield final_event
                    return
                run_span.set_attribute("cache.hit", False)

                graph, config, graph_input = await self._prepare(
                    workflow,
                    payload,
                    policy,
                    run_id=run_id,
                    resume_with=resume_with,
                    principal=principal,
                )
                final_state: Mapping[str, Any] | None = None

                async for raw_event in graph.astream_events(graph_input, config):
                    mapped_event, state, spent = self._map_event(
                        raw_event, run_id, run_span, node_spans
                    )
                    if state is not None:
                        final_state = state
                    if spent is not None:
                        # Accumulated across every model call in the graph, so
                        # a multi-node workflow meters the whole run (AC-5).
                        usage = usage + spent
                    if mapped_event is not None:
                        await self._log(workflow.name, mapped_event)
                        yield mapped_event

                # A pause is neither a result nor a failure. Checked before the
                # output contract, because the terminal state of a paused graph
                # is *partial* — validating it is the R0 defect (AC-1).
                gate = await self._pending_interrupt(graph, config)
                if gate is not None:
                    run_span.set_attribute("run.paused", True)
                    await self._mark(run_id, "awaiting_decision", gate)
                    interrupt_event = InterruptEvent(run_id=run_id, payload=gate)
                    await self._log(workflow.name, interrupt_event)
                    yield interrupt_event
                    return

                if final_state is None:  # pragma: no cover - defensive
                    raise RuntimeError(f"workflow {workflow.name!r} produced no terminal state")

                output = validate_output(workflow.Output, workflow.extract_output(final_state))
                body = output.model_dump(mode="json")
                await self._store(key, body, workflow.cache_ttl_s)
                await self._mark(run_id, "completed")
                final_event = FinalEvent(run_id=run_id, output=body, cached=False)
                await self._log(workflow.name, final_event)
                yield final_event

            except ContractError as exc:
                run_span.set_attribute("error.kind", "contract")
                await self._mark(run_id, "failed")
                error_event = ErrorEvent(
                    run_id=run_id, error="contract_error", detail=exc.as_payload()
                )
                await self._log(workflow.name, error_event)
                yield error_event
            except Exception as exc:
                run_span.set_attribute("error.kind", type(exc).__name__)
                await self._mark(run_id, "failed")
                error_event = ErrorEvent(
                    run_id=run_id,
                    error=type(exc).__name__,
                    detail={"message": str(exc)},
                )
                await self._log(workflow.name, error_event)
                yield error_event
            finally:
                # Runs on client disconnect too (aclose), so spans never leak
                # and every run is metered exactly once (AC-5).
                for span in node_spans.values():
                    span.end()
                node_spans.clear()
                self.observability.cost_meter.record(run_id, workflow.name, policy.model, usage)

    # ------------------------------------------------------------------ helpers

    async def _prepare(
        self,
        workflow: Workflow[Any, Any],
        payload: Any,
        policy: Policy,
        *,
        run_id: str,
        resume_with: Mapping[str, Any] | None,
        principal: Principal | None,
    ) -> tuple[Any, dict[str, Any], Any]:
        """Build the graph and decide what to feed it: fresh state, or a resume."""
        deps = self.deps.with_(policy=policy, llm=self.client_for(policy))
        graph = workflow.build_graph(deps)
        self._require_checkpointer(workflow, graph)
        config: dict[str, Any] = {
            "callbacks": list(self.observability.callbacks),
            "configurable": {"thread_id": run_id},
            "run_name": workflow.name,
        }
        if resume_with is not None:
            return graph, config, Command(resume=dict(resume_with))

        return graph, config, workflow.initial_state(payload)

    def _require_checkpointer(self, workflow: Workflow[Any, Any], graph: Any) -> None:
        """A workflow that can pause must be able to persist where it paused.

        Failing loudly here beats losing a run at the gate: without a
        checkpointer the interrupt has nowhere to live and the resume could
        never find it.
        """
        if workflow.checkpointed and getattr(graph, "checkpointer", None) is None:
            raise RuntimeError(
                f"workflow {workflow.name!r} declares checkpointed=True but its graph "
                f"was compiled without a checkpointer; it could pause and never resume"
            )

    async def _pending_interrupt(
        self, graph: Any, config: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The gate payload if the graph is paused, else `None`.

        A post-stream state check rather than an inference from event shapes:
        an interrupted graph emits `on_chain_start` for the gate node with no
        matching end, which is not something to pattern-match on.
        """
        if getattr(graph, "checkpointer", None) is None:
            return None
        snapshot = await graph.aget_state(config)
        for task in getattr(snapshot, "tasks", ()) or ():
            for pending in getattr(task, "interrupts", ()) or ():
                value = getattr(pending, "value", None)
                return value if isinstance(value, dict) else {"value": value}
        return None

    async def _open_run(
        self,
        run_id: str,
        workflow: Workflow[Any, Any],
        policy: Policy,
        principal: Principal | None,
    ) -> None:
        if self.run_store is None:
            return
        await self.run_store.create_run(
            run_id=run_id,
            workflow=workflow.name,
            policy={"model": policy.model, "fingerprint": policy.fingerprint()},
            created_by=principal,
        )

    async def _mark(
        self, run_id: str, state: RunState, gate_payload: dict[str, Any] | None = None
    ) -> None:
        if self.run_store is None:
            return
        with suppress(RunNotFoundError):
            await self.run_store.mark_state(run_id, state, gate_payload)

    async def _log(self, workflow: str, event: Event) -> None:
        if self.run_log_store is None or isinstance(event, TokenEvent):
            return
        if isinstance(event, NodeEvent):
            await self.run_log_store.append(
                run_id=event.run_id,
                workflow=workflow,
                step=event.node,
                status=event.status,
            )
        elif isinstance(event, InterruptEvent):
            await self.run_log_store.append(
                run_id=event.run_id, workflow=workflow, status="awaiting_decision"
            )
        elif isinstance(event, ErrorEvent):
            await self.run_log_store.append(
                run_id=event.run_id,
                workflow=workflow,
                status="failed",
                detail={"error": event.error},
            )
        elif isinstance(event, FinalEvent):
            await self.run_log_store.append(
                run_id=event.run_id,
                workflow=workflow,
                status="completed",
                detail={"cached": event.cached},
            )

    def _cache_key(self, workflow: Workflow[Any, Any], payload: Any, policy: Policy) -> str | None:
        if self.cache is None or not workflow.idempotent:
            return None
        return cache_key(workflow.name, payload.model_dump(mode="json"), policy)

    async def _lookup(self, key: str | None) -> dict[str, Any] | None:
        if key is None or self.cache is None:
            return None
        return await self.cache.get(key)

    async def _store(self, key: str | None, body: dict[str, Any], ttl_s: int | None) -> None:
        if key is None or self.cache is None:
            return
        await self.cache.set(key, body, ttl_s)

    def _map_event(
        self,
        raw: Mapping[str, Any],
        run_id: str,
        run_span: Span,
        node_spans: dict[str, Span],
    ) -> tuple[Event | None, Mapping[str, Any] | None, Usage | None]:
        """The only place LangGraph's event shape is known.

        Returns `(event_to_emit, terminal_state_if_any, usage_if_any)`. A
        LangChain version bump touches this function and nothing else.
        """
        kind = raw.get("event")
        name = str(raw.get("name", ""))
        data = raw.get("data") or {}

        # Streaming comes free from the chat model — no node cooperation.
        if kind == "on_chat_model_stream":
            text = text_of(data.get("chunk"))
            return (TokenEvent(run_id=run_id, text=text) if text else None), None, None

        if kind == "on_chat_model_end":
            spent = usage_of(data.get("output"))
            return None, None, Usage(*spent)

        if _is_node(raw):
            event_run_id = str(raw.get("run_id", ""))
            if kind == "on_chain_start":
                span = self.observability.tracer.start_span(
                    f"node.{name}", context=trace.set_span_in_context(run_span)
                )
                node_spans[event_run_id] = span
                return NodeEvent(run_id=run_id, node=name, status="started"), None, None
            if kind == "on_chain_end":
                if event_run_id in node_spans:
                    node_spans.pop(event_run_id).end()
                return NodeEvent(run_id=run_id, node=name, status="completed"), None, None

        if kind == "on_chain_end" and not raw.get("parent_ids"):
            output = data.get("output")
            return None, (output if isinstance(output, Mapping) else None), None

        return None, None, None


def _is_node(raw: Mapping[str, Any]) -> bool:
    """True for the chain events LangGraph emits around a graph node itself."""
    metadata = raw.get("metadata") or {}
    node = metadata.get("langgraph_node")
    return bool(node) and node == raw.get("name")
