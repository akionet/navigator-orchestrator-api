"""Versioned prompt registry, boot-validated (SPEC-AIP-002 §3.4, C-7, AC-4).

Prompts are **data**: `prompts/<id>/<version>.md` with YAML front-matter. A
missing or renamed prompt fails at startup, never mid-run — that is AC-4, and
it is why `validate_all` exists as a separate step the app lifespan calls.

Business rules do not live in prose here; they live in code validators.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

import yaml

__all__ = [
    "MissingPromptError",
    "Prompt",
    "PromptError",
    "PromptRegistry",
    "PromptRenderError",
    "parse_ref",
]

_REF = re.compile(r"^(?P<id>[a-z0-9][a-z0-9_-]*)@(?P<version>[0-9]+)$")
_FRONT_MATTER = re.compile(r"\A---\r?\n(?P<meta>.*?)\r?\n---\r?\n(?P<body>.*)\Z", re.DOTALL)


class PromptError(Exception):
    """Base for anything wrong with prompt data."""


class MissingPromptError(PromptError):
    """A referenced `id@version` does not resolve. Raised at boot (AC-4)."""


class PromptRenderError(PromptError):
    """Render was called without a declared input."""


def parse_ref(ref: str) -> tuple[str, int]:
    """Split `"echo@1"` into `("echo", 1)`."""
    match = _REF.match(ref)
    if match is None:
        raise PromptError(f"malformed prompt ref {ref!r}; expected '<id>@<version>'")
    return match["id"], int(match["version"])


@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    version: int
    inputs: tuple[str, ...]
    template: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def render(self, **values: Any) -> str:
        missing = [name for name in self.inputs if name not in values]
        if missing:
            raise PromptRenderError(f"{self.ref} needs {', '.join(sorted(missing))}")
        return self.template.format_map({name: values[name] for name in self.inputs})


@dataclass(slots=True)
class PromptRegistry:
    """All prompts on disk, loaded once at boot."""

    _prompts: dict[str, Prompt]

    @classmethod
    def from_dir(cls, root: Path) -> PromptRegistry:
        if not root.is_dir():
            raise MissingPromptError(f"prompt directory {root} does not exist")
        prompts: dict[str, Prompt] = {}
        for path in sorted(root.glob("*/*.md")):
            prompt = _load_file(path)
            prompts[prompt.ref] = prompt
        return cls(prompts)

    def load(self, ref: str) -> Prompt:
        parse_ref(ref)  # reject malformed refs before reporting "missing"
        try:
            return self._prompts[ref]
        except KeyError as exc:
            known = ", ".join(sorted(self._prompts)) or "none"
            raise MissingPromptError(f"prompt {ref!r} is not on disk (have: {known})") from exc

    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._prompts))

    def validate_all(self, required: Iterable[str]) -> None:
        """Fail fast at startup for every prompt the app claims it will use.

        Checks resolution *and* that each declared input is actually
        satisfiable from the template — a renamed placeholder is as broken as
        a missing file, and both should surface before the first request.
        """
        missing: list[str] = []
        for ref in sorted(set(required)):
            try:
                prompt = self.load(ref)
            except MissingPromptError as exc:
                missing.append(str(exc))
                continue
            placeholders = _placeholders(prompt.template)
            undeclared = placeholders - set(prompt.inputs)
            unused = set(prompt.inputs) - placeholders
            if undeclared:
                missing.append(f"{ref}: template uses undeclared input(s) {sorted(undeclared)}")
            if unused:
                missing.append(f"{ref}: declares unused input(s) {sorted(unused)}")
        if missing:
            detail = "\n  ".join(missing)
            raise MissingPromptError(f"prompt registry validation failed:\n  {detail}")


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def _load_file(path: Path) -> Prompt:
    match = _FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise PromptError(f"{path} has no YAML front-matter block")
    meta = yaml.safe_load(match["meta"]) or {}
    if not isinstance(meta, dict):
        raise PromptError(f"{path} front-matter must be a mapping")
    try:
        prompt_id = str(meta["id"])
        version = int(meta["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PromptError(f"{path} front-matter needs `id` and integer `version`") from exc

    expected = f"{prompt_id}/{version}.md"
    actual = f"{path.parent.name}/{path.name}"
    if expected != actual:
        raise PromptError(f"{path} declares {expected} but lives at {actual}")

    raw_inputs = meta.get("inputs") or []
    if not isinstance(raw_inputs, list):
        raise PromptError(f"{path} front-matter `inputs` must be a list")
    return Prompt(
        id=prompt_id,
        version=version,
        inputs=tuple(str(name) for name in raw_inputs),
        template=match["body"].strip(),
    )
