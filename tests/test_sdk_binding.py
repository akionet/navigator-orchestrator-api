"""Hook binding by name over a superset (SPEC-NSP-001 AC-2/AC-3)."""

from __future__ import annotations

from typing import Any

import pytest

from navigator_orchestrator.sdk.binding import (
    BindingError,
    bind_kwargs,
    declared_parameters,
    takes_var_keyword,
)

ALLOWED = ("question", "documents", "sources")
POOL = {"question": "why?", "documents": ["d"], "sources": ["s"], "unrelated": 1}


def test_a_hook_may_declare_a_subset() -> None:
    def answer(ctx: Any, question: str) -> None: ...

    assert bind_kwargs(answer, available=POOL, allowed=ALLOWED) == {"question": "why?"}


def test_a_hook_may_declare_everything() -> None:
    def answer(ctx: Any, question: str, documents: list[str], sources: list[str]) -> None: ...

    bound = bind_kwargs(answer, available=POOL, allowed=ALLOWED)
    assert set(bound) == {"question", "documents", "sources"}


def test_var_keyword_receives_the_whole_allowed_set() -> None:
    def answer(ctx: Any, **kw: Any) -> None: ...

    bound = bind_kwargs(answer, available=POOL, allowed=ALLOWED)
    assert set(bound) == set(ALLOWED)
    assert "unrelated" not in bound, "the pool is wider than the step's declared kwargs"


def test_a_hook_never_receives_keys_outside_the_step_declaration() -> None:
    """The pool holds `unrelated`; the step does not offer it, so nobody gets it."""

    def answer(ctx: Any, question: str) -> None: ...

    assert "unrelated" not in bind_kwargs(answer, available=POOL, allowed=ALLOWED)


def test_ctx_is_dropped_whatever_it_is_called() -> None:
    def answer(context: Any, question: str) -> None: ...

    assert declared_parameters(answer) == (("question", False),)


def test_defaults_are_reported() -> None:
    def answer(ctx: Any, question: str, example: str | None = None) -> None: ...

    assert declared_parameters(answer) == (("question", False), ("example", True))


def test_an_absent_optional_is_simply_not_passed() -> None:
    def answer(ctx: Any, question: str, missing: str = "fallback") -> None: ...

    assert bind_kwargs(answer, available=POOL, allowed=(*ALLOWED, "missing")) == {
        "question": "why?"
    }


def test_an_absent_required_parameter_is_an_error() -> None:
    def answer(ctx: Any, absent: str) -> None: ...

    with pytest.raises(BindingError, match="absent"):
        bind_kwargs(answer, available=POOL, allowed=(*ALLOWED, "absent"))


def test_var_keyword_is_detected() -> None:
    def with_kw(ctx: Any, **kw: Any) -> None: ...

    def without_kw(ctx: Any) -> None: ...

    assert takes_var_keyword(with_kw)
    assert not takes_var_keyword(without_kw)


def test_adding_a_kwarg_does_not_break_an_existing_hook() -> None:
    """AC-3, in miniature. The tagged scenario lives in `nsp-authoring.feature`.

    `old` was written when a step offered only `question`. The step now offers
    `locale` as well. The hook must keep binding, unchanged.
    """

    def old(ctx: Any, question: str) -> None: ...

    before = bind_kwargs(old, available=POOL, allowed=("question",))
    after = bind_kwargs(old, available={**POOL, "locale": "en-GB"}, allowed=("question", "locale"))
    assert before == after == {"question": "why?"}
