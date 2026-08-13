"""AC-1, AC-2, AC-7 — `features/engine-runtime.feature`."""

from __future__ import annotations

import inspect
from typing import Any

from conftest import running_app
from pytest_bdd import given, parsers, scenarios, then, when

from features.steps._support import collect, final_of, of_type
from navigator_orchestrator.engine.policy import Policy
from navigator_orchestrator.workflows.echo import EchoOutput
from navigator_orchestrator.workflows.echo import nodes as echo_nodes

scenarios("engine-runtime.feature")


@given(parsers.parse('the "{name}" workflow is registered'))
def workflow_is_registered(context: Any, name: str) -> None:
    assert name in context.registry.names()


@given(parsers.parse('the model policy is "{model}"'))
def model_policy_is(bdd_context: dict[str, Any], model: str) -> None:
    bdd_context["policy"] = Policy(model=model)


@when('I run "echo" with input {"text": "ping"}')
def run_echo(runner: Any, bdd_context: dict[str, Any], run_async: Any) -> None:
    stream = runner.run("echo", {"text": "ping"}, bdd_context.get("policy"))
    bdd_context["events"] = run_async(collect(stream))


@when(parsers.parse('I run "echo" with policy "{model}"'))
def run_echo_with_policy(
    runner: Any,
    bdd_context: dict[str, Any],
    run_async: Any,
    model: str,
) -> None:
    stream = runner.run("echo", {"text": "ping"}, Policy(model=model))
    bdd_context.setdefault("runs", []).append(run_async(collect(stream)))


@when(parsers.parse('I GET "{path}"'))
def get_path(app: Any, bdd_context: dict[str, Any], run_async: Any, path: str) -> None:
    async def _call() -> None:
        async with running_app(app) as client:
            bdd_context["response"] = await client.get(path)

    run_async(_call())


@then('I receive streamed "token" events')
def streamed_tokens(bdd_context: dict[str, Any]) -> None:
    tokens = of_type(bdd_context["events"], "token")
    assert tokens, "no token events were streamed"
    node_events = of_type(bdd_context["events"], "node")
    assert node_events, "no node events were streamed"


@then("a final event whose output validates against EchoOutput")
def final_validates(bdd_context: dict[str, Any]) -> None:
    final = final_of(bdd_context["events"])
    EchoOutput.model_validate(final.output)


@then(parsers.parse('the final output text is "{text}"'))
def final_text_is(bdd_context: dict[str, Any], text: str) -> None:
    assert final_of(bdd_context["events"]).output["text"] == text


@then("both runs succeed with identical node code paths")
def identical_node_paths(bdd_context: dict[str, Any]) -> None:
    first, second = (final_of(run) for run in bdd_context["runs"])
    assert first.output["text"] == second.output["text"]
    # The swap really happened...
    assert first.output["model"] != second.output["model"]
    # ...and the node has no provider-specific branch to have taken (AC-2).
    source = inspect.getsource(echo_nodes)
    for provider in ("bedrock", "anthropic", "vertex", "boto3"):
        assert provider not in source.lower(), f"node module mentions {provider!r}"


@then(parsers.parse("the status is {status:d}"))
def status_is(bdd_context: dict[str, Any], status: int) -> None:
    assert bdd_context["response"].status_code == status


@then('the body reports "engine", "postgres", "redis" states')
def body_reports_states(bdd_context: dict[str, Any]) -> None:
    body = bdd_context["response"].json()
    assert set(body) >= {"engine", "postgres", "redis"}
    assert body["engine"]["state"] == "ok"
    assert "echo" in body["engine"]["workflows"]
    assert body["postgres"]["state"] in {"ok", "disabled", "unavailable"}
    assert body["redis"]["state"] in {"ok", "disabled", "unavailable"}
