"""SPEC-NSP-007 runtime schema synchronization and validation scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import AnyUrl, TypeAdapter
from pytest_bdd import given, scenarios, then, when

from navigator_orchestrator.sdk.project import load_project
from navigator_orchestrator.sdk.schema import validate_instance
from navigator_orchestrator.sdk.schema_sources import load_locked_schema, sync_schema

scenarios("runtime-schema-sync.feature", "runtime-schema-validation.feature")


@given("a project with a local runtime schema")
@given("a synchronized runtime schema")
def local_schema(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    (tmp_path / "contract.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "sourceDocuments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"url": {"type": "string", "format": "uri"}},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "navigator-orchestrator.toml").write_text(
        """
[schemas.submission]
backend = "local"
method = "POST"
path = "contract.json"
source = "file"
""",
        encoding="utf-8",
    )
    project = load_project(tmp_path)
    bdd_context["project"] = project
    bdd_context["snapshot"] = sync_schema(project, "submission")


@when("the runtime schema is synchronized")
def synchronized(bdd_context: dict[str, Any]) -> None:
    bdd_context["snapshot"] = sync_schema(bdd_context["project"], "submission")


@when("a candidate contains a Pydantic URL object")
def candidate_has_url_object(bdd_context: dict[str, Any]) -> None:
    url = TypeAdapter(AnyUrl).validate_python("https://example.com/source")
    bdd_context["result"] = validate_instance(
        bdd_context["snapshot"], {"sourceDocuments": [{"url": url}]}
    )


@then("a content-addressed schema snapshot is locked")
def snapshot_is_locked(bdd_context: dict[str, Any]) -> None:
    snapshot = bdd_context["snapshot"]
    target = (
        bdd_context["project"].root
        / ".navigator-orchestrator"
        / "schemas"
        / "submission"
        / f"{snapshot.revision}.json"
    )
    assert target.is_file()


@then("loading the contract offline returns the same revision")
def offline_revision_matches(bdd_context: dict[str, Any]) -> None:
    locked = load_locked_schema(bdd_context["project"], "submission")
    assert locked.revision == bdd_context["snapshot"].revision


@then("validation fails at the nested URL JSON Pointer")
def url_finding_is_precise(bdd_context: dict[str, Any]) -> None:
    result = bdd_context["result"]
    assert not result.valid
    assert result.findings[0].path == "/sourceDocuments/0/url"
