"""`Workflow` ABC + registry (SPEC-AIP-002 §3.2, TODO-1).

A capability is a thin plugin: typed Pydantic I/O at the edges, a LangGraph
built from injected `Deps`, versioned prompt refs, and a BDD suite. Everything
reusable lives in the engine, so a new workflow adds a package — never an
engine edit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.state import BaseState, new_state

__all__ = ["UnknownWorkflowError", "Workflow", "WorkflowRegistry", "WorkflowSource"]


class UnknownWorkflowError(KeyError):
    """No workflow is registered under that name. Surfaces as HTTP 404."""

    status_code = 404


@dataclass(frozen=True, slots=True)
class WorkflowSource:
    """Trusted source metadata captured when a workflow is registered.

    The API may expose YAML verbatim, but it never accepts a path from a
    browser. Code-defined workflows deliberately carry no source text: turning
    a compiled graph back into YAML would invent a definition nobody authored.
    """

    kind: Literal["yaml", "python"]
    logical_name: str
    text: str | None = None

    def __post_init__(self) -> None:
        if not self.logical_name.strip():
            raise ValueError("workflow source logical_name must not be empty")
        if self.kind == "yaml" and self.text is None:
            raise ValueError("a YAML workflow source must carry its exact text")
        if self.kind == "python" and self.text is not None:
            raise ValueError("a Python workflow source must not expose source text")

    @classmethod
    def python(cls, workflow: Workflow[Any, Any]) -> WorkflowSource:
        workflow_type = type(workflow)
        return cls(
            kind="python",
            logical_name=f"{workflow_type.__module__}:{workflow_type.__qualname__}",
        )


class Workflow[TIn: BaseModel, TOut: BaseModel](ABC):
    """One registered capability."""

    #: Route segment and cache-key component. Unique across the registry.
    name: ClassVar[str] = ""
    #: Edge contracts — validated by the Runner, never inside the graph.
    Input: type[TIn]
    Output: type[TOut]
    #: Only opt in when identical input genuinely implies identical output.
    idempotent: ClassVar[bool] = False
    cache_ttl_s: ClassVar[int | None] = None
    #: House rule: don't over-checkpoint short-lived graphs.
    checkpointed: ClassVar[bool] = False
    #: Every `id@version` this workflow may load, validated at boot (AC-4).
    prompt_refs: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def build_graph(self, deps: Deps) -> Any:
        """Return a compiled LangGraph with `deps` bound into its nodes."""

    def initial_state(self, payload: TIn) -> BaseState:
        return new_state(input=payload.model_dump())

    def extract_output(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        """Pull the Output-shaped mapping out of the final graph state."""
        scratch = state.get("scratch") or {}
        output = scratch.get("output")
        if not isinstance(output, Mapping):
            raise ValueError(
                f"workflow {self.name!r} produced no output in state['scratch']['output']"
            )
        return output


@dataclass(slots=True)
class WorkflowRegistry:
    """Name → workflow. Duplicate registration is an error, not a silent replace."""

    _workflows: dict[str, Workflow[Any, Any]] = field(default_factory=dict)
    _sources: dict[str, WorkflowSource] = field(default_factory=dict)

    def register(
        self,
        workflow: Workflow[Any, Any],
        *,
        source: WorkflowSource | None = None,
    ) -> Workflow[Any, Any]:
        if not workflow.name:
            raise ValueError(f"{type(workflow).__name__} must set a non-empty `name`")
        if workflow.name in self._workflows:
            raise ValueError(f"workflow {workflow.name!r} is already registered")
        self._workflows[workflow.name] = workflow
        self._sources[workflow.name] = source or WorkflowSource.python(workflow)
        return workflow

    def get(self, name: str) -> Workflow[Any, Any]:
        try:
            return self._workflows[name]
        except KeyError as exc:
            known = ", ".join(self.names()) or "none"
            raise UnknownWorkflowError(f"unknown workflow {name!r} (registered: {known})") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._workflows))

    def source(self, name: str) -> WorkflowSource:
        self.get(name)  # preserve the registry's useful unknown-workflow error
        return self._sources[name]

    def prompt_refs(self) -> tuple[str, ...]:
        refs = {ref for wf in self._workflows.values() for ref in wf.prompt_refs}
        return tuple(sorted(refs))

    def __iter__(self) -> Iterator[Workflow[Any, Any]]:
        return iter(self._workflows.values())

    def __len__(self) -> int:
        return len(self._workflows)
