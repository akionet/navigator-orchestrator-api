"""`uses`, `ctx.skip`, `summary_keys` and the `shell` executor (SPEC-NSP-005).

The two leak tests are the ones that matter most here. Both leaks were engine
code that knew record field names, and the second recorded *any other domain's*
skipped step as `ok`. So both are tested with a domain that is deliberately not
records — a payroll step and a bakery step — because a test written in the
vocabulary of the leak cannot detect the leak.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.sdk.context import Ctx, FileAccess
from navigator_orchestrator.sdk.execution import resolve_hook, summarise_product
from navigator_orchestrator.sdk.registry import (
    UnknownImplementationError,
    clear_implementations,
    implementation,
    register_implementation,
    resolve_uses,
)
from navigator_orchestrator.sdk.runner import StepFailed, run_template
from navigator_orchestrator.sdk.shell import ShellFailed, env_for, run_shell_step
from navigator_orchestrator.sdk.templates import Step, Template
from navigator_orchestrator.store.events import InMemoryEventLog


@pytest.fixture
def deps() -> Deps:
    """No prompts: nothing here reaches a model, and a step that quietly started
    to would rather fail than pass on a fake."""
    return Deps(prompts=None, llm=FakeChatModel(model_name="fake:echo"))


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Module-level state is exactly as sticky as it looks."""
    clear_implementations()
    yield
    clear_implementations()


def ctx_for(tmp_path: Path, deps: Deps, **params: Any) -> Ctx:
    return Ctx(params=params, deps=deps, files=FileAccess(root=tmp_path))


def one_step(step: Step, **params: Any) -> Template:
    return Template(name="t", steps=(step,), params=tuple(params))


# ── uses ─────────────────────────────────────────────────────────────────────


def test_uses_resolves_to_the_registered_implementation() -> None:
    def draft(ctx: Ctx) -> str:
        return "drafted"

    register_implementation("client.draft", draft)
    fn, source = resolve_hook(Step("draft", "agent", produces="d", uses="client.draft"), {})

    assert fn is draft
    assert source == "uses"


def test_the_decorator_returns_the_function_unchanged() -> None:
    """Registration is a side effect, not a wrapper: the function stays directly
    callable and directly testable."""

    @implementation("bakery.proof")
    def proof(ctx: Ctx) -> str:
        return "proofed"

    assert resolve_uses("bakery.proof") is proof
    assert proof(None) == "proofed"  # type: ignore[arg-type]


def test_a_file_hook_beats_uses() -> None:
    """The author's override always wins — that is what makes a template a
    starting point rather than a cage."""
    register_implementation("client.draft", lambda ctx: "registered")

    def draft(ctx: Ctx) -> str:
        return "mine"

    fn, source = resolve_hook(
        Step("draft", "agent", produces="d", uses="client.draft"), {"draft": draft}
    )
    assert (fn, source) == (draft, "file")


def test_an_unknown_uses_names_what_is_registered() -> None:
    """A `uses` typo is the likeliest failure this indirection introduces, so it
    has to be the easiest one to diagnose."""
    register_implementation("client.draft", lambda ctx: None)

    with pytest.raises(StepFailed) as caught:
        resolve_hook(Step("draft", "agent", produces="d", uses="client.drat"), {})

    assert "client.drat" in str(caught.value)
    assert "client.draft" in str(caught.value)
    assert isinstance(caught.value.cause, UnknownImplementationError)


def test_duplicate_registration_is_an_error_not_a_replace() -> None:
    register_implementation("client.draft", lambda ctx: "first")
    with pytest.raises(ValueError, match="already registered"):
        register_implementation("client.draft", lambda ctx: "second")


@pytest.mark.parametrize("name", ["draft", "Client.Draft", "client draft", "record.", ""])
def test_a_malformed_name_is_refused_at_registration(name: str) -> None:
    """At import, not at the step that needed it half an hour into a run."""
    with pytest.raises(ValueError, match="valid implementation name"):
        register_implementation(name, lambda ctx: None)


def test_a_step_cannot_declare_both_uses_and_default() -> None:
    with pytest.raises(ValueError, match="pick one"):
        Step("draft", "agent", produces="d", uses="client.draft", default=lambda ctx: None)


def test_a_step_with_uses_is_not_required() -> None:
    """`uses` counts as an implementation, which is the whole point of it."""
    assert Step("draft", "agent", produces="d", uses="client.draft").required is False
    assert Step("draft", "agent", produces="d").required is True


def test_a_gate_step_needs_no_hook() -> None:
    """The engine implements gates. `check` previously demanded a hook for one,
    which would have failed every template with a human in it."""
    assert Step("review", "gate", produces="verdict").required is False


async def test_a_run_reaches_a_registered_implementation(tmp_path: Path, deps: Deps) -> None:
    """End to end: nothing in the template holds a callable."""

    @implementation("payroll.total")
    def total(ctx: Ctx, hours: int) -> dict[str, Any]:
        return {"pay": hours * 12}

    template = one_step(
        Step("total", "local", produces="paid", kwargs=("hours",), uses="payroll.total"),
        hours=0,
    )
    result = await run_template(template, {}, ctx_for(tmp_path, deps, hours=10))

    assert result.pool["paid"] == {"pay": 120}
    assert result.steps[0].source == "uses"


# ── leak 1: ctx.skip ─────────────────────────────────────────────────────────


async def test_a_skipped_step_in_another_domain_is_recorded_as_skipped(
    tmp_path: Path, deps: Deps
) -> None:
    """The leak, stated as a test.

    Before `ctx.skip`, `skipped` was inferred from `produced["enrichment"]`, so
    this payroll step — which plainly skipped — was recorded as `ok`. Written in
    a non-record vocabulary on purpose: a test using the leaked field name
    cannot detect the leak.
    """

    def reconcile(ctx: Ctx) -> dict[str, Any]:
        ctx.skip("no ledger export for this period")
        return {"rows": 0}

    events = InMemoryEventLog()
    template = one_step(Step("reconcile", "local", produces="reconciled"))
    await run_template(template, {"reconcile": reconcile}, ctx_for(tmp_path, deps), events=events)

    row = next(e for e in events.entries if e["step"] == "reconcile" and e["status"] != "started")
    assert row["status"] == "skipped"
    assert row["detail"]["reason"] == "no ledger export for this period"


async def test_a_skip_does_not_leak_into_the_next_step(tmp_path: Path, deps: Deps) -> None:
    """`ctx` outlives one step. A skip left set would mark every later step
    skipped too, which is the failure mode that makes the log worse than none."""

    def first(ctx: Ctx) -> str:
        ctx.skip("nothing to do")
        return "a"

    def second(ctx: Ctx) -> str:
        return "b"

    events = InMemoryEventLog()
    template = Template(
        name="t",
        steps=(
            Step("first", "local", produces="one"),
            Step("second", "local", produces="two"),
        ),
    )
    await run_template(
        template, {"first": first, "second": second}, ctx_for(tmp_path, deps), events=events
    )

    statuses = {e["step"]: e["status"] for e in events.entries if e["status"] != "started"}
    assert statuses["first"] == "skipped"
    assert statuses["second"] == "ok"


# ── leak 2: summary_keys ─────────────────────────────────────────────────────


def test_the_summariser_surfaces_only_the_declared_keys() -> None:
    product = {"loaves": 12, "oven": "deck", "crumb": {"open": True}, "secret": "x"}
    detail = summarise_product(product, ("loaves", "oven", "crumb"))

    assert detail["loaves"] == 12
    assert detail["oven"] == "deck"
    assert detail["crumb"] == 1, "a nested value is counted, never copied"
    assert "secret" not in detail, "only declared keys are surfaced"


def test_the_summariser_no_longer_knows_record_field_names() -> None:
    """Without `summary_keys` it reports shape and nothing else — the property
    that makes it a *generic* summariser (SPEC-NSP-005 §3)."""
    assert summarise_product({"score": 91, "title": "Dal"}) == {"keys": ["score", "title"]}


def test_a_declared_key_that_is_absent_is_simply_absent() -> None:
    assert "score" not in summarise_product({"title": "Dal"}, ("score", "title"))


# ── shell ────────────────────────────────────────────────────────────────────


def test_a_shell_step_without_a_command_is_refused_at_declaration() -> None:
    """The command comes from the template. A step that does not carry one would
    have to get it from somewhere else, and there is nowhere safe."""
    with pytest.raises(ValueError, match="remote code execution"):
        Step("deploy", "shell", produces="out")


def test_a_non_shell_step_cannot_carry_a_command() -> None:
    with pytest.raises(ValueError, match="but is a"):
        Step("draft", "agent", produces="d", command=("echo", "hi"))


def test_pool_values_reach_the_command_as_environment_not_as_arguments() -> None:
    """This is the constraint that makes the rest safe: a model's output can
    contain anything, and as an environment variable it is still just data."""
    env = env_for({"title": "$(rm -rf /); drop table", "rows": 3, "big": [1, 2]}, ("title", "rows"))

    assert env == {"NAV_TITLE": "$(rm -rf /); drop table", "NAV_ROWS": "3"}
    assert "NAV_BIG" not in env, "only scalars; a list has no obvious rendering"


async def test_a_shell_step_runs_and_captures_stdout(tmp_path: Path, deps: Deps) -> None:
    step = Step(
        "greet",
        "shell",
        produces="out",
        command=(sys.executable, "-c", "print('hello from the step')"),
    )
    produced = await run_shell_step(step, ctx_for(tmp_path, deps), {})

    assert produced["exit"] == 0
    assert "hello from the step" in produced["stdout"]


async def test_a_shell_step_sees_declared_pool_values(tmp_path: Path, deps: Deps) -> None:
    step = Step(
        "echo",
        "shell",
        produces="out",
        kwargs=("title",),
        command=(sys.executable, "-c", "import os; print(os.environ['NAV_TITLE'])"),
    )
    produced = await run_shell_step(step, ctx_for(tmp_path, deps), {"title": "Dal Tadka"})

    assert "Dal Tadka" in produced["stdout"]


async def test_a_non_zero_exit_fails_the_step(tmp_path: Path, deps: Deps) -> None:
    """Silence on failure is what produces 'but the workflow said it published
    it' a week later."""
    step = Step(
        "fail",
        "shell",
        produces="out",
        command=(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('no such bucket'); sys.exit(3)",
        ),
    )
    with pytest.raises(ShellFailed) as caught:
        await run_shell_step(step, ctx_for(tmp_path, deps), {})

    assert "exited 3" in str(caught.value)
    assert "no such bucket" in str(caught.value)


async def test_a_hanging_command_is_killed_at_the_timeout(tmp_path: Path, deps: Deps) -> None:
    step = Step(
        "hang",
        "shell",
        produces="out",
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        timeout=0.5,
    )
    with pytest.raises(ShellFailed, match="timeout"):
        await run_shell_step(step, ctx_for(tmp_path, deps), {})


async def test_a_missing_binary_says_so(tmp_path: Path, deps: Deps) -> None:
    step = Step("nope", "shell", produces="out", command=("navigator-orchestrator-does-not-exist",))
    with pytest.raises(ShellFailed, match="cannot run"):
        await run_shell_step(step, ctx_for(tmp_path, deps), {})


async def test_a_failing_shell_step_fails_the_run_and_is_logged(tmp_path: Path, deps: Deps) -> None:
    """Dispatched by the runner, not called directly — the wiring is the point."""
    events = InMemoryEventLog()
    template = one_step(
        Step("fail", "shell", produces="out", command=(sys.executable, "-c", "raise SystemExit(2)"))
    )
    with pytest.raises(StepFailed):
        await run_template(template, {}, ctx_for(tmp_path, deps), events=events)

    row = next(e for e in events.entries if e["step"] == "fail" and e["status"] == "failed")
    assert row["detail"]["error"] == "ShellFailed"


async def test_a_shell_step_needs_no_hook(tmp_path: Path, deps: Deps) -> None:
    events = InMemoryEventLog()
    template = one_step(
        Step("ok", "shell", produces="out", command=(sys.executable, "-c", "print(1)"))
    )
    result = await run_template(template, {}, ctx_for(tmp_path, deps), events=events)

    assert result.steps[0].source == "engine"
    assert result.pool["out"]["exit"] == 0
