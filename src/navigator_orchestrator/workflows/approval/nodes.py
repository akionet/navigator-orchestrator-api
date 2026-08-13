"""`approval`'s nodes (SPEC-AIP-003 §3.6).

The gate is the interesting one. `interrupt()` suspends the graph mid-node;
the checkpointer persists it; a `Command(resume=…)` from any process — days
later, by a different person — returns from that call with the decision. The
node reads as straight-line code, which is the whole appeal.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import text_of
from navigator_orchestrator.engine.state import BaseState

__all__ = ["PROMPT_REF", "decide_node", "gate_node", "propose_node"]

PROMPT_REF = "approval@1"


async def propose_node(state: BaseState, deps: Deps) -> dict[str, Any]:
    """Draft something for a human to rule on."""
    if deps.prompts is None:  # pragma: no cover - the app always injects one
        raise RuntimeError("propose_node needs deps.prompts; check the composition root")

    scratch = state.get("scratch") or {}
    payload = scratch.get("input") or {}
    rendered = deps.prompts.load(PROMPT_REF).render(text=payload["text"])
    message = await deps.llm.ainvoke([HumanMessage(content=rendered)])

    return {"scratch": {**scratch, "proposal": text_of(message)}}


async def gate_node(state: BaseState, deps: Deps) -> dict[str, Any]:
    """Pause for a human. Everything after this line runs in a later process."""
    scratch = state.get("scratch") or {}
    decision = interrupt(
        {
            "proposal": scratch.get("proposal", ""),
            "model": deps.policy.model,
            "prompt_ref": PROMPT_REF,
        }
    )
    return {"scratch": {**scratch, "decision": decision}}


async def decide_node(state: BaseState, deps: Deps) -> dict[str, Any]:
    """Shape the decision into the Output contract. No judgement of its own."""
    scratch = state.get("scratch") or {}
    decision = scratch.get("decision") or {}
    return {
        "scratch": {
            **scratch,
            "output": {
                "proposal": scratch.get("proposal", ""),
                "verdict": decision.get("verdict", "reject"),
                "comment": decision.get("comment"),
                "decided_by": decision.get("decided_by", "unknown"),
            },
        }
    }
