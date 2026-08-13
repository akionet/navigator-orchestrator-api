"""`EchoWorkflow` — the reference registration (SPEC-AIP-002 §3.9)."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.state import BaseState
from navigator_orchestrator.engine.workflow import Workflow
from navigator_orchestrator.workflows.echo.contracts import EchoInput, EchoOutput
from navigator_orchestrator.workflows.echo.nodes import PROMPT_REF, echo_node

__all__ = ["EchoWorkflow"]


class EchoWorkflow(Workflow[EchoInput, EchoOutput]):
    name = "echo"
    Input = EchoInput
    Output = EchoOutput
    #: Opted in purely to make AC-6 assertable — echoing is genuinely idempotent.
    idempotent = True
    cache_ttl_s = 300
    prompt_refs = (PROMPT_REF,)

    def __init__(self, checkpointer: Any | None = None) -> None:
        # `echo` runs checkpointer-off in the app (short-lived graph). The
        # parameter exists so the resumability smoke can prove the seam.
        self._checkpointer = checkpointer

    @property
    def checkpointed(self) -> bool:  # type: ignore[override]
        return self._checkpointer is not None

    def build_graph(self, deps: Deps) -> Any:
        graph = StateGraph(BaseState)
        graph.add_node("echo", partial(echo_node, deps=deps))
        graph.add_edge(START, "echo")
        graph.add_edge("echo", END)
        return graph.compile(checkpointer=self._checkpointer)
