from __future__ import annotations

from pydantic import AnyUrl, TypeAdapter

from navigator_orchestrator import (
    SchemaContractError,
    SchemaRef,
    make_schema_snapshot,
    schema_fingerprint,
    validate_instance,
)


def _snapshot(schema: dict):
    return make_schema_snapshot(
        SchemaRef(
            id="submission",
            backend="client-service",
            method="POST",
            path="/v1/submission",
            source="openapi",
        ),
        schema,
    )


def test_validates_nested_arrays_enums_formats_unions_and_local_refs() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "link": {
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string", "format": "uri"}},
                "additionalProperties": False,
            }
        },
        "type": "object",
        "required": ["diet", "links", "score"],
        "properties": {
            "diet": {"enum": ["sanctions", "vegetarian"]},
            "links": {"type": "array", "items": {"$ref": "#/$defs/link"}},
            "score": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        },
        "additionalProperties": False,
    }
    snapshot = _snapshot(schema)

    result = validate_instance(
        snapshot,
        {"diet": "sanctions", "links": [{"url": "https://example.com/record"}], "score": None},
    )

    assert result.valid
    assert result.findings == ()

    extra = validate_instance(
        snapshot,
        {
            "diet": "sanctions",
            "links": [{"url": "https://example.com/record", "unknown": True}],
            "score": 1,
        },
    )
    assert not extra.valid
    assert extra.findings[0].path == "/links/0"
    assert extra.findings[0].keyword == "additionalProperties"


def test_returns_deterministic_json_pointer_findings() -> None:
    snapshot = _snapshot(
        {
            "type": "object",
            "required": ["title", "links"],
            "properties": {
                "title": {"type": "string"},
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"url": {"type": "string", "format": "uri"}},
                        "required": ["url"],
                    },
                },
            },
        }
    )

    result = validate_instance(snapshot, {"title": 12, "links": [{"url": "not a URI"}]})

    assert not result.valid
    assert [(finding.path, finding.keyword) for finding in result.findings] == [
        ("/links/0/url", "format"),
        ("/title", "type"),
    ]


def test_rejects_pydantic_url_without_coercing_it() -> None:
    snapshot = _snapshot({"type": "object"})
    url = TypeAdapter(AnyUrl).validate_python("https://example.com/records/123")

    result = validate_instance(snapshot, {"sourceDocuments": [{"url": url}]})

    assert not result.valid
    assert result.findings[0].path == "/sourceDocuments/0/url"
    assert result.findings[0].keyword == "json-serializable"
    assert "Url" in result.findings[0].message


def test_rejects_non_finite_numbers_and_non_string_keys() -> None:
    result = validate_instance(_snapshot({"type": "object"}), {1: "one", "score": float("nan")})

    assert not result.valid
    assert {finding.path for finding in result.findings} == {"", "/score"}


def test_fingerprint_is_independent_of_mapping_order() -> None:
    assert schema_fingerprint({"type": "object", "required": ["x"]}) == schema_fingerprint(
        {"required": ["x"], "type": "object"}
    )


def test_snapshot_rejects_invalid_schema_and_revision_drift() -> None:
    ref = SchemaRef(
        id="record",
        backend="local",
        method="POST",
        path="record.schema.json",
        source="file",
    )

    try:
        make_schema_snapshot(ref, {"type": "definitely-not-a-type"})
    except SchemaContractError as exc:
        assert "invalid JSON Schema" in str(exc)
    else:
        raise AssertionError("invalid schema was accepted")

    pinned = ref.model_copy(update={"revision": "0" * 64})
    try:
        make_schema_snapshot(pinned, {"type": "object"})
    except SchemaContractError as exc:
        assert "revision mismatch" in str(exc)
    else:
        raise AssertionError("revision drift was accepted")
