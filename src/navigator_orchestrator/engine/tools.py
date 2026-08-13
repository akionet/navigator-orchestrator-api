"""Tool/DataSource protocol with capability metadata (SPEC-AIP-002 §3.10).

R0 ships the **protocol only** plus `echo`'s no-op tool. The capability tags
and the `writable` guard exist now so R3's connector registry (SPEC-NLQ-002)
and R6's polyglot retriever (SPEC-UMB-002) compose without an engine edit, and
so every write into an `navigator` system stays API-mediated and gated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "Capability",
    "DataSource",
    "Tool",
    "ToolRegistry",
    "ToolSpec",
    "WriteNotPermittedError",
]

Capability = Literal["semantic", "graph", "relational", "document"]


class WriteNotPermittedError(RuntimeError):
    """A caller asked a read-only tool to write. Guardrail, not a feature flag."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What a tool is, in terms the engine can route on without knowing the domain."""

    name: str
    description: str
    capabilities: tuple[Capability, ...] = ()
    #: Writes are opt-in and, per the repo guardrails, only ever
    #: API-mediated + attestation-gated. Nothing at R0 sets this.
    writable: bool = False


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class DataSource(Protocol):
    spec: ToolSpec

    async def fetch(self, query: str, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class ToolRegistry:
    """Name → tool, with capability lookup for later releases' routers."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"tool {tool.spec.name!r} is already registered")
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool {name!r}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def by_capability(self, capability: Capability) -> tuple[Tool, ...]:
        return tuple(t for _, t in sorted(self._tools.items()) if capability in t.spec.capabilities)

    def require_writable(self, name: str) -> Tool:
        tool = self.get(name)
        if not tool.spec.writable:
            raise WriteNotPermittedError(f"tool {name!r} is read-only")
        return tool
