"""Addressable step implementations — `uses` (SPEC-NSP-005 §5).

A `Template` is already declarative in every field but one: `Step.default` holds
a Python callable, and that single field is what stands between the Python
templates written today and a YAML workflow file written later. Naming
implementations instead — as GitHub Actions does with `uses:` — makes that later
step a *parser* rather than a redesign, and means no template written between
now and then has to be rewritten.

```python
Step("draft", "agent", uses="client.draft")     # today
```
```yaml
- step: draft                                   # later, same resolution
  uses: client.draft
```

**This is a bet, and it is recorded as one** (`SPEC-NSP-005` §5.1). If workflows
only ever get authored by people who could equally have written the Python, the
indirection is ceremony. Falsified if, after two more templates, every `uses`
string still has exactly one call site and no workflow file has been authored by
a non-programmer — at which point drop it and keep `default=`.

**The unplanned benefit.** `uses` forces implementations to be a named,
registered, *closed* set. `SPEC-WFB-001` §4 said data-defined workflows would
need exactly that before they could be safe. The analogy and the old objection
turn out to agree.

## Names

`namespace.verb`, lowercase, dots separating segments. The namespace is what
keeps two workflow projects from colliding once implementations arrive from more
than one place — `client.draft` and `payroll.draft` are different things and
should not have to negotiate.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "UnknownImplementationError",
    "clear_implementations",
    "implementation",
    "known_implementations",
    "register_implementation",
    "resolve_uses",
]

#: `namespace.verb`, or deeper. Enforced at registration so a typo surfaces at
#: import rather than at the step that needed it, half an hour into a run.
NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")

_IMPLEMENTATIONS: dict[str, Callable[..., Any]] = {}


class UnknownImplementationError(KeyError):
    """No implementation is registered under that `uses` name."""


def register_implementation(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register `fn` under `name`.

    Duplicate registration is an **error, not a replace** — the same posture as
    `TemplateRegistry`. Silently winning the last import is how two workflows
    that look identical behave differently.
    """
    if not NAME.match(name):
        raise ValueError(
            f"{name!r} is not a valid implementation name; expected "
            f"'namespace.verb' in lowercase, e.g. 'client.draft'"
        )
    existing = _IMPLEMENTATIONS.get(name)
    if existing is not None and existing is not fn:
        raise ValueError(
            f"implementation {name!r} is already registered "
            f"(by {getattr(existing, '__module__', '?')})"
        )
    _IMPLEMENTATIONS[name] = fn
    return fn


def implementation(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator form. The function is returned unchanged, so it stays directly
    callable and directly testable — registration is a side effect, not a wrapper.

    ```python
    @implementation("client.draft")
    async def draft(ctx, brief): ...
    ```
    """

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        return register_implementation(name, fn)

    return register


def resolve_uses(name: str) -> Callable[..., Any]:
    """The implementation registered under `name`.

    The error lists what *is* registered. A `uses` typo is the most likely
    failure this indirection introduces, so it pays for itself by being the
    easiest one to diagnose.
    """
    try:
        return _IMPLEMENTATIONS[name]
    except KeyError as exc:
        known = ", ".join(sorted(_IMPLEMENTATIONS)) or "none"
        raise UnknownImplementationError(
            f"unknown implementation {name!r} (registered: {known})"
        ) from exc


def known_implementations() -> Mapping[str, Callable[..., Any]]:
    """A snapshot, for `navigator-orchestrator check` and for tests."""
    return dict(_IMPLEMENTATIONS)


def clear_implementations() -> None:
    """Empty the registry. For tests only; module-level state is otherwise
    exactly as sticky as it looks."""
    _IMPLEMENTATIONS.clear()
