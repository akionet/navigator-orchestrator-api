"""Runner behaviour beyond the BDD happy paths (SPEC-AIP-002 §3.5, §3.13)."""

from __future__ import annotations

from typing import Any

import pytest

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.policy import Policy
from navigator_orchestrator.engine.runner import Runner
from navigator_orchestrator.engine.state import ContractError
from navigator_orchestrator.engine.workflow import UnknownWorkflowError, WorkflowRegistry
from navigator_orchestrator.workflows.echo import EchoWorkflow


async def collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


def test_unknown_workflow_raises_before_streaming(runner: Runner) -> None:
    """Raised at call time so the API can answer 404 instead of a half-stream."""
    with pytest.raises(UnknownWorkflowError):
        runner.run("nope", {"text": "ping"})


def test_invalid_input_raises_before_streaming(runner: Runner, fake_llm: Any) -> None:
    with pytest.raises(ContractError):
        runner.run("echo", {"wrong": 1})
    assert fake_llm.calls == 0


async def test_node_exception_becomes_an_error_event(runner: Runner, observability: Any) -> None:
    class _Exploding(FakeChatModel):
        """Fails on both paths — LangChain may take either depending on streaming."""

        def _generate(
            self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
        ) -> Any:
            raise RuntimeError("provider exploded")

        def _stream(
            self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
        ) -> Any:
            raise RuntimeError("provider exploded")

    runner.client_factory = lambda _policy: _Exploding()
    runner._clients.clear()
    events = await collect(runner.run("echo", {"text": "ping"}))

    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    assert "exploded" in errors[0].detail["message"]
    assert not [e for e in events if e.type == "final"], "a failed run must not emit final"
    # §3.13: the cost meter still records the run.
    assert len(observability.cost_meter.for_run(errors[0].run_id)) == 1


async def test_output_contract_failure_is_reported_not_streamed(runner: Runner) -> None:
    """A run whose output fails validation is a failed run (§3.13)."""

    class BadOutput(EchoWorkflow):
        def extract_output(self, state: Any) -> Any:
            return {"text": "ping"}  # missing `model` and `tokens`

    registry = WorkflowRegistry()
    registry.register(BadOutput())
    runner.registry = registry

    events = await collect(runner.run("echo", {"text": "ping"}))
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    assert errors[0].error == "contract_error"
    assert errors[0].detail["direction"] == "output"


async def test_caching_is_skipped_for_non_idempotent_workflows(
    runner: Runner, fake_llm: Any
) -> None:
    class NotIdempotent(EchoWorkflow):
        idempotent = False

    registry = WorkflowRegistry()
    registry.register(NotIdempotent())
    runner.registry = registry

    await collect(runner.run("echo", {"text": "ping"}))
    await collect(runner.run("echo", {"text": "ping"}))
    assert fake_llm.calls == 2


async def test_a_different_policy_is_a_different_cache_entry(runner: Runner, fake_llm: Any) -> None:
    await collect(runner.run("echo", {"text": "ping"}, Policy(model="fake:echo")))
    await collect(runner.run("echo", {"text": "ping"}, Policy(model="fake:echo-alt")))
    assert fake_llm.calls == 2


async def test_events_carry_a_single_run_id(runner: Runner) -> None:
    events = await collect(runner.run("echo", {"text": "ping"}))
    assert len({e.run_id for e in events}) == 1


async def test_node_events_bracket_the_tokens(runner: Runner) -> None:
    events = await collect(runner.run("echo", {"text": "ping"}))
    kinds = [e.type for e in events]
    assert kinds[0] == "node"
    assert kinds[-1] == "final"
    assert "token" in kinds
    started = next(e for e in events if e.type == "node")
    assert started.status == "started"


async def test_runner_without_a_cache_still_runs(
    context: Any, fake_llm: Any, observability: Any
) -> None:
    registry = WorkflowRegistry()
    registry.register(EchoWorkflow())
    deps = Deps(llm=fake_llm, prompts=context.prompts, policy=Policy(model="fake:echo"))
    runner = Runner(
        registry, deps, observability, cache=None, default_policy=Policy(model="fake:echo")
    )
    events = await collect(runner.run("echo", {"text": "ping"}))
    assert next(e for e in events if e.type == "final").cached is False
