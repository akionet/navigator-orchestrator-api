"""Resolve and lock runtime write contracts (SPEC-NSP-007 S2)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from navigator_orchestrator.sdk.project import Project, ProjectError
from navigator_orchestrator.sdk.schema import (
    SchemaContractError,
    SchemaRef,
    SchemaSnapshot,
    make_schema_snapshot,
)

LOCK_VERSION = 1
STATE_DIR = ".navigator-orchestrator"
LOCK_NAME = "schema-lock.json"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HTTP_NOT_MODIFIED = 304


def sync_schema(
    project: Project,
    name: str,
    *,
    client: httpx.Client | None = None,
) -> SchemaSnapshot:
    """Resolve one configured schema and atomically update its local lock."""

    ref = project.schema(name)
    if not SAFE_ID.fullmatch(ref.id):
        raise SchemaContractError(f"unsafe schema id {ref.id!r}")

    if ref.source == "file":
        document = _read_project_schema(project, ref.path)
        etag = None
    elif ref.source == "openapi":
        document, etag, not_modified = _fetch_openapi(project, ref, client=client)
        if not_modified:
            return load_locked_schema(project, name)
        document = resolve_openapi_request_schema(document, ref.method, ref.path)
    else:
        raise SchemaContractError(
            "effective-contract endpoint sources are reserved until an owning service exposes one"
        )

    snapshot = make_schema_snapshot(ref, document)
    _write_snapshot(project, snapshot, etag=etag)
    return snapshot


def load_locked_schema(project: Project, name: str) -> SchemaSnapshot:
    """Load a pinned schema without network access."""

    entry = _lock_entries(project).get(name)
    if not isinstance(entry, dict):
        raise SchemaContractError(
            f"schema {name!r} has no lock; run `navigator-orchestrator schema sync {name}`"
        )
    relative = entry.get("snapshot")
    if not isinstance(relative, str):
        raise SchemaContractError(f"schema lock entry {name!r} has no snapshot")
    target = _confined(project.root, project.root / relative)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaContractError(f"cannot read locked schema {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaContractError(f"locked schema {target} is not an object")

    ref = project.schema(name)
    expected = str(entry.get("revision", ""))
    pinned = ref.model_copy(update={"revision": expected})
    return make_schema_snapshot(pinned, raw)


def schema_lock_entry(project: Project, name: str) -> dict[str, Any]:
    entry = _lock_entries(project).get(name)
    if not isinstance(entry, dict):
        raise SchemaContractError(f"schema {name!r} has no lock")
    return dict(entry)


def resolve_openapi_request_schema(
    document: dict[str, Any], method: str, path: str
) -> dict[str, Any]:
    """Extract an application/json request body and bundle component refs."""

    try:
        operation = document["paths"][path][method.lower()]
        content = operation["requestBody"]["content"]
        media = content.get("application/json") or content.get("application/*+json")
        schema = media["schema"]
    except (KeyError, TypeError) as exc:
        raise SchemaContractError(
            f"OpenAPI has no application/json request schema for {method.upper()}:{path}"
        ) from exc
    if not isinstance(schema, dict):
        raise SchemaContractError(
            f"OpenAPI request schema for {method.upper()}:{path} is not an object"
        )

    components = document.get("components", {}).get("schemas", {})
    if not isinstance(components, dict):
        raise SchemaContractError("OpenAPI components.schemas is not an object")
    bundled: dict[str, Any] = {}
    active: set[str] = set()

    def rewrite(value: Any) -> Any:
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key != "$ref":
                result[key] = rewrite(child)
                continue
            if not isinstance(child, str) or not child.startswith("#/"):
                raise SchemaContractError(f"external OpenAPI $ref is not allowed: {child!r}")
            prefix = "#/components/schemas/"
            if not child.startswith(prefix):
                raise SchemaContractError(f"unsupported local OpenAPI $ref: {child}")
            component_name = _unescape_pointer(child[len(prefix) :])
            if component_name not in components:
                raise SchemaContractError(f"OpenAPI $ref does not exist: {child}")
            safe_name = f"openapi__{component_name}"
            result[key] = f"#/$defs/{_escape_pointer(safe_name)}"
            if safe_name not in bundled and safe_name not in active:
                active.add(safe_name)
                bundled[safe_name] = rewrite(components[component_name])
                active.remove(safe_name)
        return result

    resolved = cast(dict[str, Any], rewrite(schema))
    existing_defs = resolved.get("$defs", {})
    if existing_defs and not isinstance(existing_defs, dict):
        raise SchemaContractError("request schema $defs is not an object")
    if bundled:
        resolved["$defs"] = {**existing_defs, **bundled}
    return resolved


def _fetch_openapi(
    project: Project, ref: SchemaRef, *, client: httpx.Client | None
) -> tuple[dict[str, Any], str | None, bool]:
    backend = project.backend(ref.backend)
    headers = {"accept": "application/json"}
    token = backend.token()
    if token:
        headers["authorization"] = f"Bearer {token[0]}"
    prior = _lock_entries(project).get(ref.id, {})
    if isinstance(prior, dict) and prior.get("etag"):
        headers["if-none-match"] = str(prior["etag"])

    owned = client is None
    http = client or httpx.Client(timeout=backend.timeout)
    try:
        response = http.get(f"{backend.base_url}/openapi.json", headers=headers)
        if response.status_code == HTTP_NOT_MODIFIED:
            return {}, None, True
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SchemaContractError(f"cannot synchronize schema {ref.id!r}: {exc}") from exc
    finally:
        if owned:
            http.close()
    if not isinstance(document, dict):
        raise SchemaContractError("OpenAPI document is not an object")
    return document, response.headers.get("etag"), False


def _read_project_schema(project: Project, declared: str) -> dict[str, Any]:
    target = _confined(project.root, project.root / declared)
    try:
        text = target.read_text(encoding="utf-8-sig")
        if target.suffix.lower() in {".yaml", ".yml"}:
            import yaml  # noqa: PLC0415

            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except (OSError, ValueError) as exc:
        raise SchemaContractError(f"cannot read schema {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaContractError(f"schema {target} is not an object")
    return raw


def _write_snapshot(project: Project, snapshot: SchemaSnapshot, *, etag: str | None) -> None:
    state = _confined(project.root, project.root / STATE_DIR)
    target = _confined(
        project.root,
        state / "schemas" / snapshot.ref.id / f"{snapshot.revision}.json",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            json.dumps(snapshot.schema_, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    lock = _read_lock(project)
    entries = lock.setdefault("schemas", {})
    entries[snapshot.ref.id] = {
        "revision": snapshot.revision,
        "snapshot": target.relative_to(project.root).as_posix(),
        "etag": etag,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    lock_path = state / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(lock, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(lock_path)


def _read_lock(project: Project) -> dict[str, Any]:
    path = project.root / STATE_DIR / LOCK_NAME
    if not path.exists():
        return {"version": LOCK_VERSION, "schemas": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaContractError(f"cannot read schema lock {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != LOCK_VERSION:
        raise SchemaContractError(f"unsupported schema lock format in {path}")
    return raw


def _lock_entries(project: Project) -> dict[str, Any]:
    entries = _read_lock(project).get("schemas", {})
    if not isinstance(entries, dict):
        raise SchemaContractError("schema lock 'schemas' must be an object")
    return entries


def _confined(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved = target.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ProjectError(f"path escapes workflow project: {target}")
    return resolved


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape_pointer(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


__all__ = [
    "load_locked_schema",
    "resolve_openapi_request_schema",
    "schema_lock_entry",
    "sync_schema",
]
