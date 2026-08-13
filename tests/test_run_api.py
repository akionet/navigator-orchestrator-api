"""The embedding API (`DESIGN-RUN-001`).

Deliberately builds its own throwaway project rather than running the sample
workflow: `test_example_workflow_is_removable.py` fails the build if anything
under `tests/` learns a workflow project's name, and it is right to. These
assertions are about the API, not about KYC.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from navigator_orchestrator import (
    RunOutcome,
    ids_from_file,
    outcomes_by_status,
    run_batch,
    run_workflow,
)

MANIFEST = """\
[paths]
templates = "templates"
flows     = "flows"
"""

TEMPLATE = """\
from typing import Any

from navigator_orchestrator.sdk.context import Ctx
from navigator_orchestrator.sdk.templates import Step, Template


def _screen(ctx: Ctx, subject: str) -> dict[str, Any]:
    if subject == "missing":
        ctx.require(False, "subject has no record")
    if subject == "listed":
        ctx.decline("subject is on the list")
    return {"subject": subject, "flagged": subject == "flagged"}


sample = Template(
    name="sample",
    doc="A throwaway workflow for testing the embedding API.",
    params=("subject",),
    steps=(
        Step(
            name="screen",
            executor="local",
            produces="screening",
            kwargs=("subject",),
            default=_screen,
        ),
        Step(
            name="review",
            executor="gate",
            produces="decision",
            when="screening.flagged",
            kwargs=("screening",),
            doc="only material when the subject was flagged",
        ),
    ),
)


def register(registry: Any) -> None:
    registry.register(sample)
"""

FLOW = 'WORKFLOW = "sample"\n'


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "navigator-orchestrator.toml").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "sample.py").write_text(textwrap.dedent(TEMPLATE), encoding="utf-8")
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "sample.py").write_text(FLOW, encoding="utf-8")
    return tmp_path


# ── the four statuses ────────────────────────────────────────────────────────


def test_a_clean_run_completes(project: Path) -> None:
    outcome = run_workflow("sample", project_dir=project, subject="ordinary")
    assert outcome.status == "completed"
    assert outcome.ok
    assert outcome.run_id, "a run id is the handle; every outcome needs one"


def test_a_gate_pauses_rather_than_raising(project: Path) -> None:
    """The decisive design point: a pause is a return value.

    Gates mean a run legitimately may not complete. Raising would put the
    *normal* case in a try/except at every call site.
    """
    outcome = run_workflow("sample", project_dir=project, subject="flagged")
    assert outcome.status == "paused"
    assert outcome.needs_human
    assert not outcome.ok
    assert outcome.gate is not None
    assert outcome.gate["step"] == "review"


def test_a_business_refusal_is_declined_not_failed(project: Path) -> None:
    """`ctx.decline` is the control working; `ctx.require` is a fault.

    Collapsing them makes a week of ordinary refusals indistinguishable from a
    week of crashes to anything watching.
    """
    outcome = run_workflow("sample", project_dir=project, subject="listed")
    assert outcome.status == "declined"
    assert "on the list" in outcome.reason
    assert not outcome.ok, "a decline is a correct outcome, but it is not a completion"


def test_a_fault_is_failed(project: Path) -> None:
    outcome = run_workflow("sample", project_dir=project, subject="missing")
    assert outcome.status == "failed"
    assert "no record" in outcome.reason


def test_an_unknown_workflow_names_what_is_available(project: Path) -> None:
    with pytest.raises(LookupError, match="sample"):
        run_workflow("nonexistent", project_dir=project, subject="ordinary")


# ── batch ────────────────────────────────────────────────────────────────────


def test_a_batch_returns_one_outcome_per_run_and_keeps_going(project: Path) -> None:
    """One decline must not stop the rest.

    A list of *results* cannot express three completions, a pause and a
    decline; a list of outcomes can, which is why the return type is what it is.
    """
    outcomes = run_batch(
        "sample",
        [{"subject": s} for s in ("ordinary", "flagged", "listed", "missing", "ordinary")],
        project_dir=project,
    )
    assert [o.status for o in outcomes] == [
        "completed",
        "paused",
        "declined",
        "failed",
        "completed",
    ]
    assert all(o.params["subject"] for o in outcomes), "each outcome carries its own parameters"


def test_outcomes_group_by_status(project: Path) -> None:
    outcomes = run_batch(
        "sample",
        [{"subject": s} for s in ("ordinary", "listed", "ordinary")],
        project_dir=project,
    )
    grouped = outcomes_by_status(outcomes)
    assert {k: len(v) for k, v in grouped.items()} == {"completed": 2, "declined": 1}


# ── reading ids from a file ──────────────────────────────────────────────────


def test_ids_from_file_skips_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "ids.txt"
    path.write_text(
        "# why this batch exists\n\nA-1\n  A-2  \n\n# trailing note\n", encoding="utf-8"
    )
    assert ids_from_file(path, param="subject") == [{"subject": "A-1"}, {"subject": "A-2"}]


def test_ids_from_file_feeds_run_batch(project: Path, tmp_path: Path) -> None:
    path = tmp_path / "subjects.txt"
    path.write_text("ordinary\nlisted\n", encoding="utf-8")
    outcomes = run_batch("sample", ids_from_file(path, param="subject"), project_dir=project)
    assert [o.status for o in outcomes] == ["completed", "declined"]


# ── the outcome type itself ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "ok", "needs_human"),
    [
        ("completed", True, False),
        ("paused", False, True),
        ("declined", False, False),
        ("failed", False, False),
    ],
)
def test_the_convenience_properties_are_narrow(status: str, ok: bool, needs_human: bool) -> None:
    outcome = RunOutcome(status, "r-1", "sample")  # type: ignore[arg-type]
    assert outcome.ok is ok
    assert outcome.needs_human is needs_human
