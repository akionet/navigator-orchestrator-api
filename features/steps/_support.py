"""Helpers shared by the step modules."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from navigator_orchestrator.events import Event

__all__ = ["collect", "final_of", "of_type"]


async def collect(stream: AsyncIterator[Event]) -> list[Event]:
    return [event async for event in stream]


def of_type(events: list[Event], kind: str) -> list[Event]:
    return [e for e in events if e.type == kind]


def final_of(events: list[Event]) -> Any:
    finals = of_type(events, "final")
    assert len(finals) == 1, f"expected exactly one final event, got {len(finals)}"
    return finals[0]
