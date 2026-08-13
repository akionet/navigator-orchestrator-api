from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navigator_orchestrator.engine.checkpoint import checkpointer_scope
from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.graph import resume_template_graph, run_template_graph
from navigator_orchestrator.sdk.project import Project, load_project
from navigator_orchestrator.sdk.runner import run_template
from navigator_orchestrator.sdk.schema import ValidationResult
from navigator_orchestrator.sdk.schema_sources import sync_schema
from navigator_orchestrator.sdk.templates import Step, Template
from navigator_orchestrator.store.events import InMemoryEventLog


@pytest.fixture
def schema_project(tmp_path: Path) -> Project:
    (tmp_path / "contract.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "navigator-orchestrator.toml").write_text(
        """
[schemas.candidate]
backend = "local"
method = "POST"
path = "contract.json"
source = "file"
""",
        encoding="utf-8",
    )
    project = load_project(tmp_path)
    sync_schema(project, "candidate")
    return project


def _ctx(project: Project, candidate: dict[str, Any] | None = None) -> Ctx:
    params = {"candidate": candidate} if candidate is not None else {}
    return Ctx(params=params, deps=Deps(), files=FileAccess(project.root), project=project)


def _validate(name: str) -> Step:
    return Step(
        name,
        "validate",
        produces=name.replace("validate", "validation"),
        schema="candidate",
        input="candidate",
    )


async def test_valid_candidate_continues_and_records_only_schema_metadata(
    schema_project: Project,
) -> None:
    template = Template(name="validate", params=("candidate",), steps=(_validate("validate"),))
    events = InMemoryEventLog()

    result = await run_template(
        template,
        {},
        _ctx(schema_project, {"title": "Apricot cake"}),
        events=events,
    )

    assert isinstance(result.output, ValidationResult)
    row = next(e for e in events.entries if e["step"] == "validate" and e["status"] == "ok")
    assert row["detail"]["schema_revision"] == result.output.revision
    assert row["detail"]["finding_count"] == 0
    assert "schema" not in row["detail"]
    assert "candidate" not in row["detail"]


async def test_invalid_candidate_blocks_before_the_following_node(
    schema_project: Project,
) -> None:
    called = False

    def publish(ctx: Ctx, validation: ValidationResult) -> str:
        nonlocal called
        called = True
        return "published"

    template = Template(
        name="validate",
        params=("candidate",),
        steps=(
            _validate("validate"),
            Step("publish", "local", produces="published", kwargs=("validation",), default=publish),
        ),
    )
    events = InMemoryEventLog()

    with pytest.raises(Blocked, match="/title"):
        await run_template(template, {}, _ctx(schema_project, {"title": 2}), events=events)

    assert not called
    blocked = next(
        e for e in events.entries if e["step"] == "validate" and e["status"] == "blocked"
    )
    assert blocked["detail"]["finding_paths"] == ["/title"]
    assert blocked["detail"]["schema_revision"]


async def test_validation_runs_before_and_after_human_review(
    schema_project: Project, tmp_path: Path
) -> None:
    template = Template(
        name="reviewed",
        params=("candidate",),
        steps=(
            _validate("validate-before-review"),
            Step("review", "gate", produces="decision", kwargs=("candidate",)),
            _validate("validate-after-review"),
        ),
    )
    events = InMemoryEventLog()
    checkpoint = tmp_path / "validation.sqlite"
    async with checkpointer_scope("sqlite", str(checkpoint)) as saver:
        paused = await run_template_graph(
            template,
            {},
            _ctx(schema_project, {"title": "Apricot cake"}),
            events=events,
            run_id="schema-gate",
            checkpointer=saver,
        )
        assert paused.is_paused
        await resume_template_graph(
            template,
            {},
            _ctx(schema_project),
            verdict={"verdict": "approve"},
            events=events,
            run_id="schema-gate",
            checkpointer=saver,
        )

    terminal_steps = [
        e["step"]
        for e in events.entries
        if e["step"].startswith("validate-") and e["status"] == "ok"
    ]
    assert terminal_steps == ["validate-before-review", "validate-after-review"]
