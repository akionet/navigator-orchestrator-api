"""AC-1 (rejection path) and AC-3 — `features/engine-contracts.feature`."""

from __future__ import annotations

from typing import Any

from conftest import running_app
from pytest_bdd import given, scenarios, then, when

from navigator_orchestrator.engine.purity import check_source

scenarios("engine-contracts.feature")

#: A node module that does exactly what AC-3 forbids.
IMPURE_NODE = """
from navigator_orchestrator.engine.llm import make_client
from navigator_orchestrator.engine.policy import Policy

CLIENT = make_client(Policy(model="fake:echo"))


async def bad_node(state, deps):
    state["scratch"]["seen"] = True
    return state
"""


@when('I run "echo" with input {"wrong": 1}')
def run_with_bad_input(app: Any, bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _call() -> None:
        async with running_app(app) as client:
            bdd_context["response"] = await client.post("/workflows/echo/runs", json={"wrong": 1})

    run_async(_call())


@then("the response is a 422 with contract errors")
def response_is_422(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["response"]
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "contract_error"
    assert body["direction"] == "input"
    assert body["detail"], "expected per-field contract errors"


@then("no graph node executed")
def no_node_executed(fake_llm: Any) -> None:
    # The only node calls the injected client; zero calls means the graph
    # never ran — validation happened at the edge, as AC-1 requires.
    assert fake_llm.calls == 0


@given("a node module that instantiates an LLM client at import time")
def impure_module(bdd_context: dict[str, Any]) -> None:
    bdd_context["source"] = IMPURE_NODE


@when("the purity check runs")
def run_purity_check(bdd_context: dict[str, Any]) -> None:
    bdd_context["violations"] = check_source(bdd_context["source"], "impure_node.py")


@then("it reports a violation")
def reports_violation(bdd_context: dict[str, Any]) -> None:
    violations = bdd_context["violations"]
    assert violations, "purity check passed an impure module"
    rules = {v.rule for v in violations}
    assert "module-level-client" in rules
    assert "state-mutation" in rules
