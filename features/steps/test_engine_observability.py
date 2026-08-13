"""AC-5 — `features/engine-observability.feature`."""

from __future__ import annotations

from typing import Any

from pytest_bdd import scenarios, then, when

from features.steps._support import collect, final_of

scenarios("engine-observability.feature")


@when('I run "echo" with input {"text": "ping"}')
def run_echo(runner: Any, bdd_context: dict[str, Any], run_async: Any) -> None:
    bdd_context["events"] = run_async(collect(runner.run("echo", {"text": "ping"})))
    bdd_context["run_id"] = final_of(bdd_context["events"]).run_id


@then("a span tree with one span per node is exported")
def span_tree_exported(bdd_context: dict[str, Any], exporter: Any) -> None:
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names.count("node.echo") == 1, f"expected one span per node, got {names}"

    run_spans = [s for s in spans if s.name == "workflow.run"]
    assert len(run_spans) == 1
    node_span = next(s for s in spans if s.name == "node.echo")
    # One tree, not two unrelated spans.
    assert node_span.parent is not None
    assert node_span.parent.span_id == run_spans[0].context.span_id
    assert node_span.context.trace_id == run_spans[0].context.trace_id


@then("exactly one cost-meter entry is recorded for the run")
def one_cost_entry(bdd_context: dict[str, Any], observability: Any) -> None:
    entries = observability.cost_meter.for_run(bdd_context["run_id"])
    assert len(entries) == 1, f"expected 1 cost entry, got {len(entries)}"
    assert entries[0].workflow == "echo"
    assert entries[0].usage.output_tokens > 0
