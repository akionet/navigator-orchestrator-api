"""Credential preflight (SPEC-NSP-003 §5.1, PLAN-NSP-R2-003 G0).

The assertion that justifies the stage is `test_the_check_runs_before_any_model_call`.
Everything else is the message being useful once it fires.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.prompts import PromptRegistry
from navigator_orchestrator.sdk import preflight
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.preflight import Requirement, describe_missing, missing_requirements
from navigator_orchestrator.sdk.runner import run_template
from navigator_orchestrator.sdk.templates import Step, Template
from navigator_orchestrator.store.events import InMemoryEventLog

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── which requirements are absent ────────────────────────────────────────────


def test_a_present_variable_is_not_missing() -> None:
    assert missing_requirements(["TOKEN"], env={"TOKEN": "abc"}) == []


def test_an_absent_variable_is_missing() -> None:
    (found,) = missing_requirements(["TOKEN"], env={})

    assert found.name == "TOKEN"


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_an_exported_but_blank_variable_counts_as_missing(value: str) -> None:
    """The usual shape of a half-finished .env. Treating it as present would
    defer the failure to the expensive step this exists to protect."""
    assert missing_requirements(["TOKEN"], env={"TOKEN": value})


def test_every_missing_variable_is_reported_not_just_the_first() -> None:
    """Fixing them one round-trip at a time is its own small misery."""
    missing = missing_requirements(["A", "B", "C"], env={"B": "set"})

    assert [r.name for r in missing] == ["A", "C"]


def test_the_message_carries_the_reason_when_one_is_given() -> None:
    message = describe_missing([Requirement("SERVICE_TOKEN", "mint one with scripts/mint")])

    assert "SERVICE_TOKEN" in message
    assert "scripts/mint" in message, "what to do, not merely what is absent"


def test_a_bare_string_requirement_is_accepted() -> None:
    assert missing_requirements(["TOKEN"], env={})[0].name == "TOKEN"


# ── the check inside a run ───────────────────────────────────────────────────


def _template(requires: tuple[Any, ...]) -> Template:
    async def _model_step(ctx: Ctx) -> str:
        return await ctx.ai.ask("this must never run when a requirement is missing")

    return Template(
        name="needs-credentials",
        doc="test fixture",
        requires=requires,
        steps=(Step("work", "agent", produces="work", default=_model_step, doc="calls the model"),),
    )


@pytest.fixture
def deps() -> Deps:
    return Deps(
        prompts=PromptRegistry.from_dir(REPO_ROOT / "prompts"),
        llm=FakeChatModel(model_name="fake:echo"),
    )


def _ctx(deps: Deps, tmp_path: Path) -> Ctx:
    return Ctx(params={}, deps=deps, files=FileAccess(root=tmp_path))


async def test_the_check_runs_before_any_model_call(
    tmp_path: Path, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of G0: a missing credential costs nothing."""
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)

    with pytest.raises(Blocked, match="SERVICE_TOKEN"):
        await run_template(_template(("SERVICE_TOKEN",)), {}, _ctx(deps, tmp_path))

    assert deps.llm.calls == 0, "the model must not be reached when a credential is absent"


async def test_a_satisfied_requirement_lets_the_run_proceed(
    tmp_path: Path, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "a-token")

    result = await run_template(_template(("SERVICE_TOKEN",)), {}, _ctx(deps, tmp_path))

    assert result.steps[-1].step == "work"


async def test_a_template_requiring_nothing_skips_the_check(tmp_path: Path, deps: Deps) -> None:
    log = InMemoryEventLog()
    await run_template(_template(()), {}, _ctx(deps, tmp_path), events=log)

    assert not [e for e in log.entries if e["step"] == "preflight"]


async def test_the_failure_is_recorded_in_the_event_log(
    tmp_path: Path, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    log = InMemoryEventLog()

    with pytest.raises(Blocked):
        await run_template(_template(("SERVICE_TOKEN",)), {}, _ctx(deps, tmp_path), events=log)

    row = [e for e in log.entries if e["step"] == "preflight"][-1]
    assert row["status"] == "blocked"
    assert row["detail"]["missing"] == ["SERVICE_TOKEN"]


async def test_a_passing_check_is_recorded_too(
    tmp_path: Path, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Visible whether it passes or fails — the same argument as validation."""
    monkeypatch.setenv("SERVICE_TOKEN", "a-token")
    log = InMemoryEventLog()

    await run_template(_template(("SERVICE_TOKEN",)), {}, _ctx(deps, tmp_path), events=log)

    row = [e for e in log.entries if e["step"] == "preflight"][-1]
    assert row["status"] == "ok"


def test_preflight_never_acquires_a_credential() -> None:
    """A workflow that can mint its own credentials can escalate its own
    privileges. Asserted against the module surface so a future 'helpful'
    addition has to argue with a test."""
    forbidden = {"mint", "acquire", "fetch", "refresh", "login", "authenticate"}

    assert not forbidden & {name.lower() for name in dir(preflight)}
