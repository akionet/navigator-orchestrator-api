"""Edge contracts and the workflow registry (SPEC-AIP-002 §3.2, TODO-1)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.state import (
    ContractError,
    new_state,
    validate_input,
    validate_output,
)
from navigator_orchestrator.engine.workflow import UnknownWorkflowError, Workflow, WorkflowRegistry
from navigator_orchestrator.workflows.echo import EchoInput, EchoOutput, EchoWorkflow


def test_valid_input_round_trips() -> None:
    assert validate_input(EchoInput, {"text": "ping"}).text == "ping"


@pytest.mark.parametrize(
    "payload",
    [{"wrong": 1}, {}, {"text": ""}, {"text": "ping", "extra": True}],
)
def test_invalid_input_raises_422(payload: dict[str, Any]) -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_input(EchoInput, payload)
    error = excinfo.value
    assert error.status_code == 422
    assert error.direction == "input"
    assert error.as_payload()["contract"] == "EchoInput"


def test_contract_errors_are_json_serializable() -> None:
    """Pydantic's `ctx` can hold arbitrary objects; the payload must survive SSE."""
    with pytest.raises(ContractError) as excinfo:
        validate_input(EchoInput, {"text": ""})
    json.dumps(excinfo.value.as_payload())  # must not raise


def test_output_contract_is_enforced() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_output(EchoOutput, {"text": "ping"})
    assert excinfo.value.direction == "output"


def test_new_state_is_empty_but_shaped() -> None:
    state = new_state(input={"text": "ping"})
    assert state["messages"] == []
    assert state["scratch"] == {"input": {"text": "ping"}}


class _Payload(BaseModel):
    text: str


class _Other(Workflow[_Payload, _Payload]):
    name = "echo"  # deliberately clashes
    Input = _Payload
    Output = _Payload

    def build_graph(self, deps: Deps) -> Any:  # pragma: no cover
        raise NotImplementedError


class _Unnamed(_Other):
    name = ""


def test_registry_registers_and_looks_up() -> None:
    registry = WorkflowRegistry()
    registry.register(EchoWorkflow())
    assert registry.names() == ("echo",)
    assert registry.get("echo").name == "echo"
    assert registry.prompt_refs() == ("echo@1",)
    assert len(registry) == 1


def test_registry_rejects_duplicates() -> None:
    registry = WorkflowRegistry()
    registry.register(EchoWorkflow())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_Other())


def test_registry_rejects_unnamed_workflow() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        WorkflowRegistry().register(_Unnamed())


def test_unknown_workflow_is_404() -> None:
    with pytest.raises(UnknownWorkflowError) as excinfo:
        WorkflowRegistry().get("nope")
    assert excinfo.value.status_code == 404


def test_extract_output_requires_a_mapping() -> None:
    with pytest.raises(ValueError, match="no output"):
        EchoWorkflow().extract_output({"scratch": {}})
