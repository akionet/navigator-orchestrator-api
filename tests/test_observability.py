"""Tracing, cost metering and the redaction seam (SPEC-AIP-002 §3.7, TODO-5)."""

from __future__ import annotations

from navigator_orchestrator.engine.observability import (
    CostMeter,
    NullRedactor,
    Observability,
    Usage,
    estimate_cost,
    langfuse_callbacks,
)


def test_cost_uses_published_per_mtok_rates() -> None:
    # Claude Opus 5: $5 / $25 per million tokens.
    cost = estimate_cost("bedrock:anthropic.claude-opus-5", Usage(1_000_000, 1_000_000))
    assert cost == 30.0


def test_cost_handles_the_bedrock_vendor_prefix() -> None:
    assert estimate_cost("bedrock:anthropic.claude-opus-5", Usage(1_000_000, 0)) == 5.0
    assert estimate_cost("anthropic:claude-opus-5", Usage(1_000_000, 0)) == 5.0


def test_unknown_model_meters_tokens_but_not_money() -> None:
    """A wrong number is worse than an absent one."""
    assert estimate_cost("fake:echo", Usage(10, 10)) is None


def test_cost_meter_scopes_entries_by_run() -> None:
    meter = CostMeter()
    meter.record("run-a", "echo", "fake:echo", Usage(1, 2))
    meter.record("run-b", "echo", "fake:echo", Usage(3, 4))
    assert len(meter.for_run("run-a")) == 1
    assert meter.for_run("run-a")[0].usage == Usage(1, 2)
    assert len(meter.entries) == 2
    meter.clear()
    assert meter.entries == []


def test_null_redactor_is_a_pass_through() -> None:
    """R0 redacts nothing; EIC-002 (R4) swaps this out without an engine edit."""
    assert NullRedactor()("sensitive") == "sensitive"


def test_langfuse_is_optional() -> None:
    assert langfuse_callbacks(None, None, None) == []
    assert langfuse_callbacks("pk", None, None) == []


def test_observability_for_tests_has_no_callbacks() -> None:
    obs = Observability.for_tests()
    assert obs.callbacks == []
    assert obs.cost_meter.entries == []


def test_span_sets_attributes(exporter: object) -> None:
    obs = Observability.for_tests([exporter])  # type: ignore[list-item]
    with obs.span("unit", **{"a.b": "c"}):
        pass
    spans = exporter.get_finished_spans()  # type: ignore[attr-defined]
    assert spans[0].name == "unit"
    assert spans[0].attributes["a.b"] == "c"
