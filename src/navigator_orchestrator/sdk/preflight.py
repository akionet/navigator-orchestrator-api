"""Fail at step zero, not step ten (SPEC-NSP-003 §5.1, G0).

A template declares the environment it cannot finish without. The check runs
first, before any model call, so a missing credential costs nothing instead of
surfacing after six paid steps and a human review.

**This never acquires a credential.** A workflow that can mint its own
credentials can escalate its own privileges, so acquisition stays an operator
action performed out of band — `client-service/scripts/mint_service_token.py`
for the SERVICE token, the Atlas console for a connection string. Preflight
only answers *"is it here?"*.

It also does not validate one against a remote. That would turn a cheap local
check into a network call with its own failure modes, and a credential that is
present but rejected is a different problem with a different message — one the
step that uses it is better placed to report.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

__all__ = ["Requirement", "describe_missing", "missing_requirements"]


class Requirement:
    """One environment variable a template needs, and why.

    The `why` is not decoration. "SERVICE_TOKEN is not set" tells an operator
    what is absent; adding "publishing to client-service needs it — mint one
    with scripts/mint_service_token.py" tells them what to do, which is the
    only reason to raise the error at all.
    """

    __slots__ = ("name", "why")

    def __init__(self, name: str, why: str = "") -> None:
        self.name = name
        self.why = why

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Requirement({self.name!r})"


def missing_requirements(
    requires: Iterable[Requirement | str],
    env: Mapping[str, str] | None = None,
) -> list[Requirement]:
    """Which requirements are absent or blank.

    A variable set to the empty string counts as missing: an exported-but-empty
    value is the usual shape of a half-finished `.env`, and treating it as
    present would defer the failure to exactly the expensive step this exists
    to protect.
    """
    source = os.environ if env is None else env
    out: list[Requirement] = []
    for item in requires:
        requirement = Requirement(item) if isinstance(item, str) else item
        if not (source.get(requirement.name) or "").strip():
            out.append(requirement)
    return out


def describe_missing(missing: Iterable[Requirement]) -> str:
    """One actionable message, listing every absent variable rather than the
    first — fixing them one round-trip at a time is its own small misery."""
    lines = []
    for requirement in missing:
        suffix = f" - {requirement.why}" if requirement.why else ""
        lines.append(f"  {requirement.name}{suffix}")
    return "this workflow needs environment variables that are not set:\n" + "\n".join(lines)
