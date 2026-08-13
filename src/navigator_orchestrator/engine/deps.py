"""`Deps` — the injection seam (SPEC-AIP-002 §3.2, AC-3).

A node is a pure `(state) -> partial update`. Everything with a side effect —
the model, tools, cache, prompts — arrives here, bound into the node by
`Workflow.build_graph`. Nodes therefore never import a client, and the purity
check enforces that mechanically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel

from navigator_orchestrator.engine.cache import Cache, InMemoryCache
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.policy import Policy
from navigator_orchestrator.engine.tools import ToolRegistry

if TYPE_CHECKING:
    from navigator_orchestrator.engine.prompts import PromptRegistry

__all__ = ["Deps"]


@dataclass(slots=True)
class Deps:
    """Everything a node is allowed to reach for.

    Two things are deliberately *absent*:

    - **No token emitter.** A node calls the chat model; LangGraph reports the
      stream as `on_chat_model_stream`, which the Runner maps to SSE.
      Streaming is a property of the model layer, not something a node opts
      into.
    - **No HITL resolver.** Human-in-the-loop is LangGraph's `interrupt()` /
      `Command(resume=...)` over the checkpointer — a node calls `interrupt`
      directly and a *different* actor resumes the thread. See
      `SPEC-ATT-001` for the maker-checker design built on it.
    """

    llm: BaseChatModel = field(default_factory=FakeChatModel)
    prompts: PromptRegistry | None = None
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    cache: Cache = field(default_factory=InMemoryCache)
    policy: Policy = field(default_factory=Policy)

    def with_(self, **overrides: Any) -> Deps:
        """A shallow copy with fields replaced — used per-run for policy/emitter."""
        return replace(self, **overrides)
