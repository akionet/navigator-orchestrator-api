"""Tracing and the token/cost meter (SPEC-AIP-002 §3.7, AC-5).

Every run produces one span tree with a span per node and **exactly one**
cost-meter entry — including runs that fail, because a failed run still spent
tokens. Langfuse is an optional exporter on top of OTel, not a second system:
if the extra is absent the traces still flow.

The redaction hook is a no-op at R0 and exists because EIC-002 (R4) handles
health-safety-sensitive text and must not retrofit it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Tracer

__all__ = [
    "CostEntry",
    "CostMeter",
    "NullRedactor",
    "Observability",
    "Redactor",
    "Usage",
    "build_tracer_provider",
    "langfuse_callbacks",
]


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts, normalised by LangChain across providers."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


# USD per million tokens (input, output). Unknown models meter tokens but not
# money — a wrong number is worse than an absent one.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class Redactor(Protocol):
    def __call__(self, value: str) -> str: ...


class NullRedactor:
    """R0 redacts nothing; the seam is what matters."""

    def __call__(self, value: str) -> str:
        return value


@dataclass(frozen=True, slots=True)
class CostEntry:
    run_id: str
    workflow: str
    model: str
    usage: Usage
    cost_usd: float | None


@dataclass(slots=True)
class CostMeter:
    """One entry per run. `record` is called exactly once, in a finally block."""

    entries: list[CostEntry] = field(default_factory=list)

    def record(self, run_id: str, workflow: str, model: str, usage: Usage) -> CostEntry:
        entry = CostEntry(
            run_id=run_id,
            workflow=workflow,
            model=model,
            usage=usage,
            cost_usd=estimate_cost(model, usage),
        )
        self.entries.append(entry)
        return entry

    def for_run(self, run_id: str) -> list[CostEntry]:
        return [e for e in self.entries if e.run_id == run_id]

    def clear(self) -> None:
        self.entries.clear()


def estimate_cost(model: str, usage: Usage) -> float | None:
    model_id = model.split(":", 1)[-1].removeprefix("anthropic.")
    price = _PRICES.get(model_id)
    if price is None:
        return None
    per_input, per_output = price
    return (usage.input_tokens * per_input + usage.output_tokens * per_output) / 1_000_000


def build_tracer_provider(exporters: Sequence[SpanExporter] = ()) -> TracerProvider:
    """A provider tests can point at an in-memory exporter (AC-5)."""
    provider = TracerProvider()
    for exporter in exporters:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


@dataclass(slots=True)
class Observability:
    """Injected into the Runner — no module-level tracer state."""

    tracer: Tracer
    cost_meter: CostMeter
    redactor: Redactor = field(default_factory=NullRedactor)
    callbacks: list[Any] = field(default_factory=list)

    @classmethod
    def for_tests(cls, exporters: Sequence[SpanExporter] = ()) -> Observability:
        provider = build_tracer_provider(exporters)
        return cls(tracer=provider.get_tracer("navigator_orchestrator"), cost_meter=CostMeter())

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        with self.tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            yield span


def default_observability(service_name: str = "navigator-orchestrator-api") -> Observability:
    """Wire the global provider once, at app startup."""
    provider = build_tracer_provider()
    trace.set_tracer_provider(provider)
    return Observability(tracer=provider.get_tracer(service_name), cost_meter=CostMeter())


def langfuse_callbacks(
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
) -> list[Any]:
    """Langfuse's LangChain handler, when the extra is installed and configured.

    Returns `[]` otherwise — observability degrades to plain OTel rather than
    taking the app down.
    """
    if not (public_key and secret_key):
        return []
    try:
        from langfuse.langchain import CallbackHandler  # noqa: PLC0415 - optional extra
    except ImportError:  # pragma: no cover - depends on install profile
        return []
    return [CallbackHandler(public_key=public_key, secret_key=secret_key, host=host)]
