"""SSE event contracts (SPEC-AIP-002 §3.5).

Four event types cross the wire — `token`, `node`, `error`, `final` — and they
are Pydantic models because the frontend (`navigator-orchestrator-app`) is a consumer of
this contract, not an implementation detail of the Runner.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ErrorEvent",
    "Event",
    "FinalEvent",
    "InterruptEvent",
    "NodeEvent",
    "TokenEvent",
    "to_sse",
]


class _BaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str


class TokenEvent(_BaseEvent):
    type: Literal["token"] = "token"
    text: str


class NodeEvent(_BaseEvent):
    type: Literal["node"] = "node"
    node: str
    status: Literal["started", "completed"]


class ErrorEvent(_BaseEvent):
    type: Literal["error"] = "error"
    error: str
    detail: dict[str, Any] = Field(default_factory=dict)


class FinalEvent(_BaseEvent):
    type: Literal["final"] = "final"
    output: dict[str, Any]
    #: True when served from the idempotent-response cache without a model call.
    cached: bool = False


class InterruptEvent(_BaseEvent):
    """The run is paused awaiting a human (SPEC-AIP-003 AC-1).

    A pause is not a failure and not a result: the stream closes cleanly with
    neither `final` nor `error`, and the run stays `awaiting_decision` until
    someone posts a decision. `payload` is whatever the workflow handed to
    `interrupt()` — opaque to the engine, rendered by the reviewer UI.
    """

    type: Literal["interrupt"] = "interrupt"
    payload: dict[str, Any]


Event = TokenEvent | NodeEvent | ErrorEvent | FinalEvent | InterruptEvent


def to_sse(event: Event) -> dict[str, str]:
    """Shape `sse_starlette.EventSourceResponse` expects."""
    return {"event": event.type, "data": event.model_dump_json()}
