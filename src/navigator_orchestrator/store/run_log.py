"""Ordered, summary-only execution logs for the operator console."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["InMemoryRunLogStore", "RunLogEntry", "RunLogStore"]


class RunLogEntry(BaseModel):
    """One safe operational transition; never model or user content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    seq: int = Field(ge=1)
    workflow: str
    step: str | None = None
    status: str
    at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class RunLogStore(Protocol):
    async def append(
        self,
        *,
        run_id: str,
        workflow: str,
        status: str,
        step: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> RunLogEntry: ...

    async def read(self, run_id: str) -> list[RunLogEntry]: ...


class InMemoryRunLogStore:
    """Process-local implementation whose lock owns sequence assignment."""

    def __init__(self) -> None:
        self._entries: dict[str, list[RunLogEntry]] = {}
        self._lock = asyncio.Lock()

    async def append(
        self,
        *,
        run_id: str,
        workflow: str,
        status: str,
        step: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> RunLogEntry:
        async with self._lock:
            entries = self._entries.setdefault(run_id, [])
            entry = RunLogEntry(
                run_id=run_id,
                seq=len(entries) + 1,
                workflow=workflow,
                step=step,
                status=status,
                at=datetime.now(UTC),
                detail=detail or {},
            )
            entries.append(entry)
            return entry

    async def read(self, run_id: str) -> list[RunLogEntry]:
        async with self._lock:
            return list(self._entries.get(run_id, ()))

    def clear(self) -> None:
        self._entries.clear()
