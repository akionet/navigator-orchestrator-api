from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from navigator_orchestrator.sdk.project import ProjectError, load_project
from navigator_orchestrator.sdk.schema import SchemaContractError, validate_instance
from navigator_orchestrator.sdk.schema_sources import (
    load_locked_schema,
    resolve_openapi_request_schema,
    sync_schema,
)

MANIFEST = """
[backends.client-service]
base_url = "https://records.example"

[schemas.submission]
backend = "client-service"
method = "POST"
path = "/v1/submission"
source = "openapi"
"""


def _openapi() -> dict:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/v1/submission": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Submission"}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Submission": {
                    "type": "object",
                    "required": ["title", "links"],
                    "properties": {
                        "title": {"type": "string"},
                        "links": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Link"},
                        },
                    },
                    "additionalProperties": False,
                },
                "Link": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri"},
                        "parent": {"$ref": "#/components/schemas/Submission"},
                    },
                    "required": ["url"],
                },
            }
        },
    }


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "navigator-orchestrator.toml").write_text(MANIFEST, encoding="utf-8")
    return tmp_path


def test_sync_bundles_nested_and_cyclic_refs_then_validates_offline(project_root: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url == "https://records.example/openapi.json"
        return httpx.Response(200, json=_openapi(), headers={"etag": '"contract-1"'})

    project = load_project(project_root)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synced = sync_schema(project, "submission", client=client)

    assert calls == 1
    assert synced.schema_["$ref"] == "#/$defs/openapi__Submission"
    assert set(synced.schema_["$defs"]) == {"openapi__Link", "openapi__Submission"}

    locked = load_locked_schema(project, "submission")
    result = validate_instance(
        locked,
        {"title": "Apricot cake", "links": [{"url": "https://example.com/source"}]},
    )
    assert result.valid
    lock = json.loads((project_root / ".navigator-orchestrator" / "schema-lock.json").read_text())
    assert lock["schemas"]["submission"]["revision"] == synced.revision


def test_etag_304_reuses_the_locked_snapshot(project_root: Path) -> None:
    project = load_project(project_root)
    responses = [
        httpx.Response(200, json=_openapi(), headers={"etag": '"contract-1"'}),
        httpx.Response(304),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if len(responses) == 1:
            assert request.headers["if-none-match"] == '"contract-1"'
        return responses.pop(0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = sync_schema(project, "submission", client=client)
        second = sync_schema(project, "submission", client=client)

    assert second.revision == first.revision


def test_missing_operation_and_external_refs_fail_closed() -> None:
    with pytest.raises(SchemaContractError, match="no application/json"):
        resolve_openapi_request_schema({"paths": {}}, "POST", "/missing")

    document = _openapi()
    document["components"]["schemas"]["Link"]["properties"]["parent"] = {
        "$ref": "https://untrusted.example/schema.json"
    }
    with pytest.raises(SchemaContractError, match="external OpenAPI"):
        resolve_openapi_request_schema(document, "POST", "/v1/submission")


def test_file_schema_cannot_escape_the_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.schema.json"
    outside.write_text('{"type":"object"}', encoding="utf-8")
    (tmp_path / "navigator-orchestrator.toml").write_text(
        """
[schemas.bad]
backend = "local"
method = "POST"
path = "../outside.schema.json"
source = "file"
""",
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match="escapes workflow project"):
        sync_schema(load_project(tmp_path), "bad")
