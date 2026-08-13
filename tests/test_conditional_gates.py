"""A gate may declare when it is material (`Step.when`).

Without this every gate stopped every run, so a reviewer confirmed eight times a
day that someone was *not* a politically exposed person. A control people learn
to click through is worse than no control, because the one real match arrives
looking exactly like the noise.

The skip is a recorded decision rather than an absence: "no human was asked, and
here is why" is a different fact from "this workflow has no gate", and an audit
trail that cannot tell them apart is not an audit trail.
"""

from __future__ import annotations

import pytest

from navigator_orchestrator.sdk.graph import gate_is_required
from navigator_orchestrator.sdk.templates import Step


def _gate(when: str = "") -> Step:
    return Step(name="review", executor="gate", produces="decision", when=when)


def test_a_gate_without_a_condition_always_pauses() -> None:
    """The prior behaviour, and the safe default."""
    assert gate_is_required(_gate(), {}) is True
    assert gate_is_required(_gate(), {"anything": False}) is True


@pytest.mark.parametrize("value", [True, "yes", 1, ["item"], {"k": "v"}])
def test_a_truthy_condition_pauses(value: object) -> None:
    assert gate_is_required(_gate("pep.is_pep"), {"pep": {"is_pep": value}}) is True


@pytest.mark.parametrize("value", [False, "", 0, [], {}, None])
def test_a_falsy_condition_skips(value: object) -> None:
    assert gate_is_required(_gate("pep.is_pep"), {"pep": {"is_pep": value}}) is False


@pytest.mark.parametrize(
    "path",
    ["pep.is_pcp", "pepp.is_pep", "pep.is_pep.deeper", "absent"],
)
def test_an_unresolvable_path_fails_closed(path: str) -> None:
    """A typo must not silently disable a compliance gate.

    A renamed `produces`, a deleted step or a mistyped key all leave `when=`
    pointing at nothing. Treating that as "immaterial" would drop the control
    *and* leave a clean record behind it — the worst available failure. The gate
    holds instead, and someone notices because the run stops.
    """
    assert gate_is_required(_gate(path), {"pep": {"is_pep": False}}) is True


def test_a_shallow_path_reads_the_top_level_pool() -> None:
    assert gate_is_required(_gate("has_art"), {"has_art": True}) is True
    assert gate_is_required(_gate("has_art"), {"has_art": False}) is False


def test_when_is_rejected_on_a_step_that_is_not_a_gate() -> None:
    """A condition that skips real work is a branch, not a gate."""
    with pytest.raises(ValueError, match="when="):
        Step(name="score", executor="local", produces="score", when="pep.is_pep")
