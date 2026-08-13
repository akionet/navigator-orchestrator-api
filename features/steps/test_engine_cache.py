"""AC-6 — `features/engine-cache.feature`."""

from __future__ import annotations

from typing import Any

from pytest_bdd import given, scenarios, then, when

from features.steps._support import collect, final_of

scenarios("engine-cache.feature")


@given('the "echo" workflow with caching enabled')
def caching_enabled(context: Any) -> None:
    assert context.settings.cache_enabled
    assert context.registry.get("echo").idempotent
    assert context.runner.cache is not None


@when('I run "echo" with input {"text": "ping"} twice')
def run_twice(runner: Any, bdd_context: dict[str, Any], run_async: Any) -> None:
    bdd_context["runs"] = [
        run_async(collect(runner.run("echo", {"text": "ping"}))) for _ in range(2)
    ]


@then("the second run returns the cached result")
def second_run_cached(bdd_context: dict[str, Any]) -> None:
    first, second = (final_of(run) for run in bdd_context["runs"])
    assert first.cached is False
    assert second.cached is True
    assert first.output == second.output


@then("the model client was called exactly once")
def called_once(fake_llm: Any) -> None:
    assert fake_llm.calls == 1
