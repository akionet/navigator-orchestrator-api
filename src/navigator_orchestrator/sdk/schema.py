"""Runtime JSON Schema contracts (SPEC-NSP-007 S1).

Domain payloads intentionally remain dictionaries.  JSON Schema is the
authority supplied by the target service; Pydantic is used only for the small,
stable navigator-orchestrator envelopes around that schema and its validation results.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError as JsonSchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field


class SchemaContractError(ValueError):
    """A schema cannot be used as a runtime contract."""


class SchemaRef(BaseModel):
    """Stable identity and provenance for a runtime write contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    source: Literal["openapi", "endpoint", "file"]
    revision: str | None = None


class SchemaSnapshot(BaseModel):
    """A resolved, immutable contract with a content-addressed revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: SchemaRef
    schema_: dict[str, Any] = Field(alias="schema")
    dialect: str
    revision: str = Field(min_length=64, max_length=64)


class ValidationFinding(BaseModel):
    """One deterministic finding at a JSON Pointer within the candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    message: str
    keyword: str


class ValidationResult(BaseModel):
    """Machine-readable result returned by every runtime contract check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    revision: str
    valid: bool
    findings: tuple[ValidationFinding, ...] = ()


def schema_fingerprint(schema: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint for semantically identical JSON."""

    try:
        canonical = json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise SchemaContractError(f"schema is not JSON-serializable: {exc}") from exc
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_schema_snapshot(ref: SchemaRef, schema: Mapping[str, Any]) -> SchemaSnapshot:
    """Validate and snapshot a resolved JSON Schema document."""

    document = dict(schema)
    validator_class = validator_for(document)
    try:
        validator_class.check_schema(document)
    except JsonSchemaError as exc:
        raise SchemaContractError(f"invalid JSON Schema: {exc.message}") from exc

    revision = schema_fingerprint(document)
    if ref.revision is not None and ref.revision != revision:
        raise SchemaContractError(
            f"schema revision mismatch: expected {ref.revision}, resolved {revision}"
        )
    dialect = document.get("$schema", validator_class.META_SCHEMA.get("$id", "unknown"))
    return SchemaSnapshot(ref=ref, schema=document, dialect=dialect, revision=revision)


def validate_instance(snapshot: SchemaSnapshot, candidate: Any) -> ValidationResult:
    """Validate one JSON-compatible candidate against a schema snapshot.

    JSON compatibility is checked first and without ``default=str``. This is
    deliberate: coercing a URL or another Python object at this boundary would
    conceal the same encoder failure that the downstream service will see.
    """

    serialization_findings: list[ValidationFinding] = []
    _find_non_json_values(candidate, "", serialization_findings)
    if serialization_findings:
        return _result(snapshot, serialization_findings)
    # Exercise the actual encoding boundary too. The recursive pass above is
    # retained because it turns an otherwise opaque TypeError into a precise
    # JSON Pointer.
    try:
        json.loads(json.dumps(candidate, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:  # defensive for unusual containers
        return _result(
            snapshot,
            [
                ValidationFinding(
                    path="",
                    message=f"candidate cannot complete a JSON round trip: {exc}",
                    keyword="json-serializable",
                )
            ],
        )

    validator_class = validator_for(snapshot.schema_)
    validator = validator_class(snapshot.schema_, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(candidate),
        key=lambda error: (_pointer(error.absolute_path), error.message, str(error.validator)),
    )
    findings = [
        ValidationFinding(
            path=_pointer(error.absolute_path),
            message=error.message,
            keyword=str(error.validator),
        )
        for error in errors
    ]
    return _result(snapshot, findings)


def _result(snapshot: SchemaSnapshot, findings: Sequence[ValidationFinding]) -> ValidationResult:
    return ValidationResult(
        schema_id=snapshot.ref.id,
        revision=snapshot.revision,
        valid=not findings,
        findings=tuple(findings),
    )


def _find_non_json_values(value: Any, path: str, findings: list[ValidationFinding]) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            findings.append(
                ValidationFinding(
                    path=path,
                    message="value is not a finite JSON number",
                    keyword="json-serializable",
                )
            )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                findings.append(
                    ValidationFinding(
                        path=path,
                        message=f"object key {key!r} is not a string",
                        keyword="json-serializable",
                    )
                )
                continue
            _find_non_json_values(child, f"{path}/{_escape_pointer(key)}", findings)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _find_non_json_values(child, f"{path}/{index}", findings)
        return
    findings.append(
        ValidationFinding(
            path=path,
            message=f"value of type {type(value).__name__} is not JSON-serializable",
            keyword="json-serializable",
        )
    )


def _pointer(parts: Sequence[object]) -> str:
    return "".join(f"/{_escape_pointer(str(part))}" for part in parts)


def _escape_pointer(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


__all__ = [
    "SchemaContractError",
    "SchemaRef",
    "SchemaSnapshot",
    "ValidationFinding",
    "ValidationResult",
    "make_schema_snapshot",
    "schema_fingerprint",
    "validate_instance",
]
