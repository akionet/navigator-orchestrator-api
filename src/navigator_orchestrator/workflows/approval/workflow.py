"""`ApprovalWorkflow` — propose → gate → decide (SPEC-AIP-003 §3.6)."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.state import BaseState
from navigator_orchestrator.engine.workflow import Workflow
from navigator_orchestrator.workflows.approval.contracts import ApprovalInput, ApprovalOutput
from navigator_orchestrator.workflows.approval.nodes import (
    PROMPT_REF,
    decide_node,
    gate_node,
    propose_node,
)

__all__ = ["ApprovalWorkflow"]


class ApprovalWorkflow(Workflow[ApprovalInput, ApprovalOutput]):
    name = "approval"
    Input = ApprovalInput
    Output = ApprovalOutput
    #: Never cache a run whose result depends on a human's judgement.
    idempotent = False
    prompt_refs = (PROMPT_REF,)

    def __init__(self, checkpointer: Any | None = None) -> None:
        # Unlike `echo`, this workflow *must* have a checkpointer: it pauses,
        # and a pause with nowhere to persist is a lost run. The Runner
        # enforces that pairing rather than trusting the caller.
        self._checkpointer = checkpointer

    @property
    def checkpointed(self) -> bool:  # type: ignore[override]
        return True

    def build_graph(self, deps: Deps) -> Any:
        graph = StateGraph(BaseState)
        graph.add_node("propose", partial(propose_node, deps=deps))
        graph.add_node("gate", partial(gate_node, deps=deps))
        graph.add_node("decide", partial(decide_node, deps=deps))
        graph.add_edge(START, "propose")
        graph.add_edge("propose", "gate")
        graph.add_edge("gate", "decide")
        graph.add_edge("decide", END)
        return graph.compile(checkpointer=self._checkpointer)
