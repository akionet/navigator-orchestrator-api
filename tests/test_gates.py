"""Gates: pause, survive the process, resume (G1c).

The test that justifies the stage is `test_a_second_process_resumes_the_run`.
Everything else could be satisfied by an in-memory pause, which proves nothing
about surviving a closed laptop — and surviving a closed laptop is the entire
reason for checkpointing rather than blocking on `input()`.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from navigator_orchestrator.engine.checkpoint import checkpointer_scope
from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.prompts import PromptRegistry
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.graph import (
    gate_payload_of,
    resume_template_graph,
    run_template_graph,
)
from navigator_orchestrator.sdk.templates import Step, Template
from navigator_orchestrator.store.events import InMemoryEventLog

REPO_ROOT = Path(__file__).resolve().parents[1]


def _gated_template() -> Template:
    """Two steps either side of a gate, so resumption has work left to do."""

    def prepare(ctx: Ctx, subject: str) -> dict[str, Any]:
        ctx.note("prepared")
        return {"subject": subject, "draft": f"a draft about {subject}"}

    def finish(ctx: Ctx, decision: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
        ctx.note("finished")
        return {"verdict": (decision or {}).get("verdict"), "title": prepared["subject"]}

    return Template(
        name="gated",
        doc="test fixture",
        params=("subject",),
        steps=(
            Step("prepare", "local", produces="prepared", kwargs=("subject",), default=prepare),
            Step(
                "review", "gate", produces="decision", kwargs=("prepared",), doc="approve the draft"
            ),
            Step(
                "finish", "local", produces="done", kwargs=("decision", "prepared"), default=finish
            ),
        ),
    )


@pytest.fixture
def deps() -> Deps:
    return Deps(
        prompts=PromptRegistry.from_dir(REPO_ROOT / "prompts"),
        llm=FakeChatModel(model_name="fake:echo"),
    )


def _ctx(deps: Deps, tmp_path: Path, **params: Any) -> Ctx:
    return Ctx(params=params, deps=deps, files=FileAccess(root=tmp_path))


# ── pausing ──────────────────────────────────────────────────────────────────


async def test_a_gate_pauses_rather_than_finishing(tmp_path: Path, deps: Deps) -> None:
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        result = await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            run_id="r1",
            checkpointer=saver,
        )

    assert result.is_paused
    assert result.paused_at == "review"
    assert "done" not in result.pool, "the step after the gate must not have run"


async def test_the_gate_payload_carries_what_the_template_declared(
    tmp_path: Path, deps: Deps
) -> None:
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        result = await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            run_id="r1",
            checkpointer=saver,
        )

    assert result.gate["step"] == "review"
    assert "prepared" in result.gate["payload"], "the step's declared kwargs"
    assert "subject" not in result.gate["payload"], "and nothing it did not declare"


async def test_a_pause_is_not_a_failure_in_the_event_log(tmp_path: Path, deps: Deps) -> None:
    log = InMemoryEventLog()
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            events=log,
            run_id="r1",
            checkpointer=saver,
        )

    statuses = {e["status"] for e in log.entries if e["step"] == "run"}
    assert "awaiting_human" in statuses
    assert not {"failed", "blocked"} & statuses


async def test_the_pause_is_recorded_once_not_twice(tmp_path: Path, deps: Deps) -> None:
    """LangGraph re-executes the interrupting node on resume. Recording inside
    the node would log every pause twice; the caller records instead."""
    log = InMemoryEventLog()
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            events=log,
            run_id="r1",
            checkpointer=saver,
        )
        await resume_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path),
            verdict={"verdict": "approve"},
            events=log,
            run_id="r1",
            checkpointer=saver,
        )

    awaiting = [e for e in log.entries if e["status"] == "awaiting_human"]
    assert len([e for e in awaiting if e["step"] == "review"]) == 1


# ── resuming ─────────────────────────────────────────────────────────────────


async def test_resume_completes_the_run(tmp_path: Path, deps: Deps) -> None:
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            run_id="r1",
            checkpointer=saver,
        )
        resumed = await resume_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path),
            verdict={"verdict": "approve"},
            run_id="r1",
            checkpointer=saver,
        )

    assert not resumed.is_paused
    assert resumed.pool["done"] == {"verdict": "approve", "title": "cake"}


async def test_notes_from_before_the_pause_survive_it(tmp_path: Path, deps: Deps) -> None:
    """A fresh `ctx` on resume contributes only this leg; state holds the rest."""
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            run_id="r1",
            checkpointer=saver,
        )
        resumed = await resume_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path),
            verdict={"verdict": "approve"},
            run_id="r1",
            checkpointer=saver,
        )

    assert resumed.notes == ["prepared", "finished"]


async def test_the_verdict_reaches_the_step_after_the_gate(tmp_path: Path, deps: Deps) -> None:
    async with checkpointer_scope("sqlite", str(tmp_path / "cp.sqlite")) as saver:
        await run_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path, subject="cake"),
            run_id="r1",
            checkpointer=saver,
        )
        resumed = await resume_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path),
            verdict={"verdict": "reject", "comment": "not seasonal"},
            run_id="r1",
            checkpointer=saver,
        )

    assert resumed.pool["decision"]["comment"] == "not seasonal"
    assert resumed.pool["done"]["verdict"] == "reject"


# ── the one that justifies checkpointing at all ──────────────────────────────


def test_a_second_process_resumes_the_run(tmp_path: Path) -> None:
    """A genuinely separate interpreter. Resuming in-process proves nothing
    about surviving a closed laptop, which is the whole reason for this stage.
    """
    db = (tmp_path / "cp.sqlite").as_posix()
    script = textwrap.dedent(f"""
        import asyncio, sys
        sys.path.insert(0, {str(REPO_ROOT / "src")!r})
        sys.path.insert(0, {str(REPO_ROOT / "tests")!r})
        from navigator_orchestrator.engine.checkpoint import checkpointer_scope
        from navigator_orchestrator.engine.deps import Deps
        from navigator_orchestrator.engine.llm import FakeChatModel
        from navigator_orchestrator.sdk.context import Ctx, FileAccess
        from navigator_orchestrator.sdk.graph import {{fn}}
        from test_gates import _gated_template
        from pathlib import Path

        async def main():
            deps = Deps(llm=FakeChatModel(model_name="fake:echo"))
            ctx = Ctx(params={{params}}, deps=deps, files=FileAccess(root=Path({str(tmp_path)!r})))
            async with checkpointer_scope("sqlite", {db!r}) as saver:
                result = await {{call}}
            print("PAUSED" if result.is_paused else "DONE:" + str(result.pool.get("done")))

        asyncio.run(main())
    """)

    start = script.format(
        fn="run_template_graph",
        params='{"subject": "cake"}',
        call='run_template_graph(_gated_template(), {}, ctx, run_id="r1", checkpointer=saver)',
    )
    resume = script.format(
        fn="resume_template_graph",
        params="{}",
        call=(
            "resume_template_graph(_gated_template(), {}, ctx, "
            'verdict={"verdict": "approve"}, run_id="r1", checkpointer=saver)'
        ),
    )

    first = subprocess.run(  # noqa: S603 - our own interpreter, our own script
        [sys.executable, "-c", start], capture_output=True, text=True, check=False
    )
    assert "PAUSED" in first.stdout, f"process 1 did not pause: {first.stderr[-800:]}"

    second = subprocess.run(  # noqa: S603 - our own interpreter, our own script
        [sys.executable, "-c", resume], capture_output=True, text=True, check=False
    )
    assert "DONE:" in second.stdout, f"process 2 did not resume: {second.stderr[-800:]}"
    assert "'verdict': 'approve'" in second.stdout


# ── running without a checkpointer ───────────────────────────────────────────


async def test_resuming_a_run_that_was_never_resumable_says_so(tmp_path: Path, deps: Deps) -> None:
    """Not "run not found" — a different problem with a different remedy."""
    with pytest.raises(Blocked, match=r"never|not be resumed|without a checkpointer"):
        await resume_template_graph(
            _gated_template(),
            {},
            _ctx(deps, tmp_path),
            verdict={"verdict": "approve"},
            run_id="r1",
            checkpointer=None,
        )


def test_the_payload_helper_surfaces_only_declared_keys() -> None:
    step = Step("review", "gate", produces="decision", kwargs=("draft",), doc="check it")

    payload = gate_payload_of(step, {"draft": "x", "secret": "y"})

    assert payload["payload"] == {"draft": "x"}
