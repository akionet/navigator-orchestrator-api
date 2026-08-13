"""Graph state and edge-contract helpers (SPEC-AIP-002 §3.2, C-5/C-6).

Schemas live at the edges: `Workflow.Input`/`Output` are Pydantic v2 models
validated by the Runner. Inside the graph the state stays deliberately small —
free text in `scratch`, never a god-object.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ValidationError

__all__ = [
    "BaseState",
    "ContractError",
    "new_state",
    "serializable_errors",
    "validate_input",
    "validate_output",
]


class BaseState(TypedDict, total=False):
    """LangGraph state. Small on purpose — see SPEC-AIP-002 §3.2."""

    messages: Annotated[list[Any], add_messages]
    scratch: dict[str, Any]


def new_state(**scratch: Any) -> BaseState:
    """Build an initial state. Nodes never mutate it in place (AC-3)."""
    return BaseState(messages=[], scratch=dict(scratch))


class ContractError(Exception):
    """An edge contract rejected a payload. Surfaces as HTTP 422.

    Raised *before* the graph runs for input, and instead of a `final` event
    for output — a run whose output fails validation is a failed run
    (SPEC-AIP-002 §3.13).
    """

    status_code = 422

    def __init__(self, direction: str, model: type[BaseModel], exc: ValidationError) -> None:
        self.direction = direction
        self.model_name = model.__name__
        self.errors = serializable_errors(exc)
        super().__init__(f"{direction} does not satisfy {self.model_name}")

    def as_payload(self) -> dict[str, Any]:
        return {
            "error": "contract_error",
            "direction": self.direction,
            "contract": self.model_name,
            "detail": self.errors,
        }


def serializable_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic error detail that survives `json.dumps`.

    A validator that raises `ValueError` leaves the exception object itself in
    `ctx`, so the raw `errors()` output is not serializable — which would turn
    a 400 into a 500 on the way out. Anything putting Pydantic errors on the
    wire goes through here.
    """
    return [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False)
    ]


def validate_input[TModel: BaseModel](model: type[TModel], raw: Any) -> TModel:
    """Validate a request payload against a Workflow's Input contract."""
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ContractError("input", model, exc) from exc


def validate_output[TModel: BaseModel](model: type[TModel], raw: Any) -> TModel:
    """Validate a graph result against a Workflow's Output contract."""
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ContractError("output", model, exc) from exc
