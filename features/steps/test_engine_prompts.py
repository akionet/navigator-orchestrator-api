"""AC-4 — `features/engine-prompts.feature`."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import running_app
from pydantic import BaseModel
from pytest_bdd import given, scenarios, then, when

from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.prompts import MissingPromptError
from navigator_orchestrator.engine.workflow import Workflow, WorkflowRegistry

scenarios("engine-prompts.feature")


class _Payload(BaseModel):
    text: str


class WorkflowWithMissingPrompt(Workflow[_Payload, _Payload]):
    """Registered but references a prompt version that was never written."""

    name = "stale"
    Input = _Payload
    Output = _Payload
    prompt_refs = ("echo@2",)

    def build_graph(self, deps: Deps) -> Any:  # pragma: no cover - never reached
        raise AssertionError("startup must fail before any graph is built")


@given('the app references prompt "echo@2" which does not exist')
def app_with_missing_prompt(bdd_context: dict[str, Any], settings: Any, fake_llm: Any) -> None:
    registry = WorkflowRegistry()
    registry.register(WorkflowWithMissingPrompt())
    bdd_context["app"] = build_app(settings, llm=fake_llm, registry=registry)


@when("the app starts")
def start_app(bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _boot() -> None:
        with pytest.raises(MissingPromptError) as excinfo:
            async with running_app(bdd_context["app"]) as client:
                bdd_context["served"] = await client.get("/healthz")
        bdd_context["error"] = excinfo.value

    run_async(_boot())


@then("startup fails with a missing-prompt error")
def startup_failed(bdd_context: dict[str, Any]) -> None:
    assert "echo@2" in str(bdd_context["error"])


@then("no request was served")
def nothing_served(bdd_context: dict[str, Any]) -> None:
    assert "served" not in bdd_context
