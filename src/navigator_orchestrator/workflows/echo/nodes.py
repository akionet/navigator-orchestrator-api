"""`echo`'s single node (SPEC-AIP-002 §3.9, AC-3).

Shape to copy for every later capability: pure `(state, deps) -> partial
update`, side effects only through `deps`, no module-level anything.

Note what is *absent* — there is no streaming code. The node calls the model
and returns; LangGraph reports the chat model's stream and the Runner turns it
into SSE. A node never has to think about transport.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import text_of, usage_of
from navigator_orchestrator.engine.state import BaseState

__all__ = ["PROMPT_REF", "echo_node"]

PROMPT_REF = "echo@1"


async def echo_node(state: BaseState, deps: Deps) -> dict[str, Any]:
    """Send the rendered prompt to the injected model and hand back an output.

    Returns a *partial* update — `state` is read, never written.
    """
    if deps.prompts is None:  # pragma: no cover - the app always injects one
        raise RuntimeError("echo_node needs deps.prompts; check the composition root")

    scratch = state.get("scratch") or {}
    payload = scratch.get("input") or {}
    rendered = deps.prompts.load(PROMPT_REF).render(text=payload["text"])

    message = await deps.llm.ainvoke([HumanMessage(content=rendered)])
    _, output_tokens = usage_of(message)

    return {
        "scratch": {
            **scratch,
            "output": {
                "text": text_of(message),
                "model": deps.policy.model,
                "tokens": output_tokens,
            },
        }
    }
