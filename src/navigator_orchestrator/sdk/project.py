"""`navigator-orchestrator.toml` — the workflow project manifest (SPEC-NSP-005 §4).

The `.github/` of this engine. A workflow project is **a directory with a
manifest**, not a Python package: found by walking up from the working
directory, exactly as `git` finds `.git` and every Python tool finds
`pyproject.toml`.

```toml
[paths]
templates = "templates"
judges    = "judges"
prompts   = "prompts"
flows     = "flows"

[backends.client-service]
base_url  = "https://api.example.com"
token_env = ["SERVICE_TOKEN", "FALLBACK_TOKEN"]
```

**Why not entry points.** They would work, and they would force every workflow
repository to become an installable package with a `pyproject.toml`, a build
backend and a reinstall after every edit. That defeats the promise the whole
design rests on: adding a judge means adding a file.

**Why walking up.** It is what makes `make respond` work from anywhere inside
the project, which is the difference between a tool people use and one they
remember to `cd` for.

Secrets are **not** here. `token_env` names an environment variable; the value
never touches a file that git can see. The manifest is committed, and everything
in it is meant to be read by anyone with the repository.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navigator_orchestrator.sdk.schema import SchemaRef

__all__ = ["Backend", "Project", "ProjectError", "find_manifest", "load_project"]

MANIFEST = "navigator-orchestrator.toml"

#: `${VAR}` or `${VAR:-fallback}` inside a manifest string.
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_PATHS = {
    "templates": "templates",
    "judges": "judges",
    "prompts": "prompts",
    "flows": "flows",
}


class ProjectError(RuntimeError):
    """The manifest is missing, unreadable, or says something impossible."""


@dataclass(frozen=True, slots=True)
class Backend:
    """One service a `service` step may call.

    Declaring backends as data is what makes "and other backends too" real
    rather than aspirational: adding one is a file, as with judges.
    """

    name: str
    base_url: str
    #: Environment variables holding a bearer token, tried in order. A list
    #: rather than one name because migrations have two valid credentials at
    #: once, and requiring a code change to switch is how migrations stall.
    token_env: tuple[str, ...] = ()
    timeout: float = 30.0

    def token(self) -> tuple[str, str] | None:
        """The first token that is set, and which variable supplied it.

        The *name* is returned so the run can record which credential it used
        without the token going anywhere near a log.
        """
        for variable in self.token_env:
            value = os.environ.get(variable)
            if value:
                return value, variable
        return None


@dataclass(frozen=True, slots=True)
class Project:
    """A loaded manifest, resolved against the directory holding it."""

    root: Path
    paths: dict[str, Path] = field(default_factory=dict)
    backends: dict[str, Backend] = field(default_factory=dict)
    schemas: dict[str, SchemaRef] = field(default_factory=dict)

    def backend(self, name: str) -> Backend:
        try:
            return self.backends[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.backends)) or "none"
            raise ProjectError(f"unknown backend {name!r} (configured: {known})") from exc

    def path(self, kind: str) -> Path:
        return self.paths.get(kind, self.root / DEFAULT_PATHS.get(kind, kind))

    def schema(self, name: str) -> SchemaRef:
        try:
            return self.schemas[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.schemas)) or "none"
            raise ProjectError(f"unknown schema {name!r} (configured: {known})") from exc


def load_project_templates(project: Project, registry: Any) -> list[str]:
    """Import every module under the project's `templates/` and register what
    it declares.

    The manifest already named this directory; until now nothing read it, which
    made `[paths] templates` a promise rather than a feature. A project's own
    templates have to be reachable or every workflow is limited to the engine's
    built-ins — which would defeat the point of a project.

    Import errors are **not** swallowed. A template that fails to import is a
    template that silently does not exist, and `WORKFLOW = "catalogue"` would
    then report "unknown workflow" and send the author hunting in the wrong
    file.
    """
    directory = project.paths.get("templates")
    if directory is None or not directory.is_dir():
        return []

    registered: list[str] = []
    # Python first, deliberately: a document's `uses` names must already be
    # registered when it is parsed, and registration happens on import.
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        for template in _templates_in(path):
            registry.register(template)
            registered.append(template.name)

    from navigator_orchestrator.sdk.document import template_from_file  # noqa: PLC0415

    for path in sorted(
        p for pattern in ("*.yaml", "*.yml", "*.json") for p in directory.glob(pattern)
    ):
        if path.name.startswith("_"):
            continue
        template = template_from_file(path)
        registry.register(template)
        registered.append(template.name)
    return registered


def _templates_in(path: Path) -> list[Any]:
    """Every `Template` a module declares at top level.

    **Cached in `sys.modules`, which is not an optimisation.** Template modules
    register their `uses` implementations at import, and registration is an
    error rather than a replace. Executing the same file twice therefore
    produced *two different function objects* under one name and raised
    "already registered" — so loading a project twice in one process (`check`
    then `run`, or two tests) failed on the second load.

    Importing the same file twice being the same module is ordinary Python
    semantics; this restores them rather than inventing anything.
    """
    import importlib.util  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from navigator_orchestrator.sdk.templates import Template  # noqa: PLC0415

    name = f"navigator_orchestrator_project.{path.stem}"
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        module = cached
    else:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ProjectError(f"cannot import template module {path}")
        module = importlib.util.module_from_spec(spec)
        # Registered *before* execution, so a template importing another one
        # does not re-enter this and start the cycle again.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            del sys.modules[name]
            raise ProjectError(f"{path}: {exc}") from exc

    return [
        value
        for name, value in vars(module).items()
        if isinstance(value, Template) and not name.startswith("_")
    ]


def find_manifest(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for `navigator-orchestrator.toml`.

    Returns `None` rather than raising: "am I in a project" is a question the
    CLI asks routinely, and not being in one is an ordinary answer.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / MANIFEST
        if candidate.is_file():
            return candidate
    return None


def load_project(start: Path | None = None) -> Project:
    """Find and parse the manifest. Raises `ProjectError` when there is none."""
    manifest = find_manifest(start)
    if manifest is None:
        searched = (start or Path.cwd()).resolve()
        raise ProjectError(
            f"no {MANIFEST} found in {searched} or any parent directory; "
            f"a workflow project is a directory with a manifest in it"
        )
    return parse_project(manifest)


def parse_project(manifest: Path) -> Project:
    try:
        data: dict[str, Any] = tomllib.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # Name the file. A parse error reported without one is a puzzle.
        raise ProjectError(f"{manifest}: {exc}") from exc

    root = manifest.parent
    declared = data.get("paths") or {}
    paths = {
        kind: (root / str(declared.get(kind, default))).resolve()
        for kind, default in DEFAULT_PATHS.items()
    }
    return Project(
        root=root,
        paths=paths,
        backends=_backends(data, manifest),
        schemas=_schemas(data, manifest),
    )


def _schemas(data: dict[str, Any], manifest: Path) -> dict[str, SchemaRef]:
    out: dict[str, SchemaRef] = {}
    for name, raw in (data.get("schemas") or {}).items():
        if not isinstance(raw, dict):
            raise ProjectError(f"{manifest}: schema {name!r} must be a table")
        try:
            out[name] = SchemaRef(
                id=name,
                backend=str(raw.get("backend", "")),
                method=str(raw.get("method", "")),
                path=str(raw.get("path", "")),
                source=str(raw.get("source", "openapi")),
                revision=str(raw["revision"]) if raw.get("revision") else None,
            )
        except ValueError as exc:
            raise ProjectError(f"{manifest}: invalid schema {name!r}: {exc}") from exc
    return out


def _backends(data: dict[str, Any], manifest: Path) -> dict[str, Backend]:
    out: dict[str, Backend] = {}
    for name, raw in (data.get("backends") or {}).items():
        if not isinstance(raw, dict):
            raise ProjectError(f"{manifest}: backend {name!r} must be a table")
        base_url = expand(str(raw.get("base_url", "")))
        if not base_url:
            raise ProjectError(f"{manifest}: backend {name!r} has no base_url")
        token_env = raw.get("token_env") or []
        if isinstance(token_env, str):
            token_env = [token_env]
        out[name] = Backend(
            name=name,
            base_url=base_url.rstrip("/"),
            token_env=tuple(str(v) for v in token_env),
            timeout=float(raw.get("timeout", 30.0)),
        )
    return out


def expand(text: str) -> str:
    """Substitute `${VAR}` and `${VAR:-fallback}` from the environment.

    Only in *values* the manifest declares, and only for configuration — a base
    URL that differs between staging and production is the case this exists for.
    An unset variable with no fallback becomes empty, which then fails the
    `base_url` check above with the backend named, rather than producing a
    request to `https:///v1/records`.
    """
    return INTERPOLATION.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)
