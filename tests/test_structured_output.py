from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.project import Project, load_project
from navigator_orchestrator.sdk.runner import run_template
from navigator_orchestrator.sdk.schema_sources import load_locked_schema, sync_schema
from navigator_orchestrator.sdk.templates import Step, Template
from navigator_orchestrator.store.events import InMemoryEventLog


class StructuredModel:
    def __init__(self, outputs: list[Any], name: str = "model-a") -> None:
        self.outputs = outputs
        self.model_name = name
        self.schemas: list[dict[str, Any]] = []

    def with_structured_output(self, schema: dict[str, Any]) -> StructuredModel:
        self.schemas.append(schema)
        return self

    async def ainvoke(self, messages: list[Any]) -> Any:
        assert messages
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


@pytest.fixture
def schema_project(tmp_path: Path) -> Project:
    (tmp_path / "candidate.json").write_text(
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
path = "candidate.json"
source = "file"
""",
        encoding="utf-8",
    )
    project = load_project(tmp_path)
    sync_schema(project, "candidate")
    return project


def _template() -> Template:
    async def draft(ctx: Ctx) -> Any:
        return await ctx.ai.ask("Create a record")

    return Template(
        name="structured",
        steps=(
            Step(
                "draft",
                "agent",
                produces="candidate",
                default=draft,
                output_schema="candidate",
            ),
        ),
    )


def _ctx(project: Project, model: StructuredModel) -> Ctx:
    return Ctx(
        params={},
        deps=Deps(llm=model),  # type: ignore[arg-type] - provider test double
        files=FileAccess(project.root),
        project=project,
    )


async def test_runtime_schema_reaches_provider_unchanged_and_result_is_revalidated(
    schema_project: Project,
) -> None:
    model = StructuredModel([{"title": "Apricot cake"}])
    result = await run_template(_template(), {}, _ctx(schema_project, model))

    locked = load_locked_schema(schema_project, "candidate")
    assert model.schemas == [locked.schema_]
    assert result.output == {"title": "Apricot cake"}


async def test_schema_invalid_provider_output_retries_once_then_succeeds(
    schema_project: Project,
) -> None:
    model = StructuredModel([{"title": 3}, {"title": "Apricot cake"}])
    events = InMemoryEventLog()

    result = await run_template(_template(), {}, _ctx(schema_project, model), events=events)

    assert result.output["title"] == "Apricot cake"
    assert len(model.schemas) == 2
    row = next(e for e in events.entries if e["step"] == "draft" and e["status"] == "ok")
    assert row["detail"]["structured_attempts"] == 2


async def test_invalid_output_after_retry_blocks_even_if_provider_claimed_success(
    schema_project: Project,
) -> None:
    model = StructuredModel([{"title": 3}, {"unexpected": True}])

    with pytest.raises(Blocked, match="schema-invalid output after 2 attempts"):
        await run_template(_template(), {}, _ctx(schema_project, model))


async def test_unparseable_output_retries_once(schema_project: Project) -> None:
    model = StructuredModel([ValueError("malformed tool result"), {"title": "Recovered"}])

    result = await run_template(_template(), {}, _ctx(schema_project, model))

    assert result.output == {"title": "Recovered"}


async def test_model_swap_changes_no_node(schema_project: Project) -> None:
    first = await run_template(
        _template(), {}, _ctx(schema_project, StructuredModel([{"title": "A"}], "model-a"))
    )
    second = await run_template(
        _template(), {}, _ctx(schema_project, StructuredModel([{"title": "B"}], "model-b"))
    )

    assert [step.step for step in first.steps] == [step.step for step in second.steps] == ["draft"]
