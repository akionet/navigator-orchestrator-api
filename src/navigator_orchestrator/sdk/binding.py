"""Bind a hook's parameters by name (SPEC-NSP-001 §4.2, AC-2/AC-3).

The rule, and the reason for it:

    The engine passes a **superset**; the hook declares any **subset**.

Binding is by parameter *name*, never by position. A hook is never called
positionally and cannot be — apart from `ctx`, which is always first and always
supplied.

This is what makes AC-3 possible. A template that gains a `locale` kwarg next
quarter must not break the workflow files already written against it, and
positional binding or strict signature equality would break every one.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["BindingError", "bind_kwargs", "declared_parameters", "takes_var_keyword"]


class BindingError(Exception):
    """A hook's signature cannot be satisfied. Raised at check time, not run time."""


def _signature(fn: Callable[..., Any]) -> inspect.Signature:
    return inspect.signature(fn)


def takes_var_keyword(fn: Callable[..., Any]) -> bool:
    """True when the hook declares `**kw` and therefore accepts everything."""
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in _signature(fn).parameters.values())


def declared_parameters(fn: Callable[..., Any]) -> tuple[tuple[str, bool], ...]:
    """`(name, has_default)` for every named parameter after `ctx`.

    `ctx` is dropped because it is positional and universal. `*args` and `**kw`
    are dropped because they name nothing — `**kw` is reported separately by
    `takes_var_keyword`.
    """
    parameters = list(_signature(fn).parameters.values())
    named = [
        p
        for p in parameters
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    # The first positional parameter is `ctx`, whatever the author called it.
    if named and named[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
        named = named[1:]
    return tuple((p.name, p.default is not inspect.Parameter.empty) for p in named)


def bind_kwargs(
    fn: Callable[..., Any],
    *,
    available: Mapping[str, Any],
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    """Compute the kwargs to call `fn` with.

    `allowed` is the step's declared superset; `available` is what the pool
    actually holds right now. A hook taking `**kw` receives every allowed key
    that is present, which is the escape hatch for the curious rather than the
    normal case.
    """
    if takes_var_keyword(fn):
        return {name: available[name] for name in allowed if name in available}

    bound: dict[str, Any] = {}
    for name, has_default in declared_parameters(fn):
        if name in available:
            bound[name] = available[name]
        elif not has_default:
            # Unreachable when `check` has run; kept so a programmatic caller
            # that skipped validation fails with a useful message.
            raise BindingError(
                f"{fn.__name__}() requires {name!r}, which the engine cannot supply here"
            )
    return bound
