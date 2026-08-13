"""Run the KYC workflow from Python, without the CLI.

    uv run --project ../.. python demo_python.py CL-0001

The CLI is a thin wrapper over exactly this. Reaching for the API directly is
what you want when embedding a run in a service, a notebook, or a batch job.

Everything here is public API — `navigator_orchestrator.__all__` — so nothing
below reaches into an internal module. `tests/test_public_api.py` fails the
build if that stops being true.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from navigator_orchestrator import Ctx, FileAccess, build_deps
from navigator_orchestrator.sdk.graph import run_template_graph
from navigator_orchestrator.sdk.loader import load_file
from navigator_orchestrator.sdk.project import load_project, load_project_templates
from navigator_orchestrator.templates import default_registry

HERE = Path(__file__).resolve().parent


async def main(client_id: str) -> int:
    project = load_project(HERE)
    registry = default_registry()
    load_project_templates(project, registry)

    parsed, _module = load_file(HERE / "flows" / "kyc.py")
    template = registry.get(parsed.workflow)

    # `prompts_dir` is the project's own; without it the run validates against
    # the engine's built-in prompts and cannot find `kyc-entities@1`.
    deps = build_deps(prompts_dir=project.paths.get("prompts"))
    ctx = Ctx(
        params={"client_id": client_id},
        deps=deps,
        files=FileAccess(root=HERE),
        project=project,
    )

    # `run_template_graph`, not `run_template`. The sequential runner has
    # nothing to interrupt, so it treats a gate as a step needing a hook and
    # fails with "required hook is not implemented". Any workflow with a gate
    # needs the graph runner — which is also what gives it a checkpointer, and
    # so the ability to resume in a different process.
    result = await run_template_graph(template, parsed.hooks, ctx)

    if result.gate:
        print(f"paused at {result.gate.get('step')}: {result.gate.get('doc')}")
        print("resume with the CLI: navigator-orchestrator decide flows/kyc.py <run-id> --approve")
        return 2

    outcome = result.pool.get("outcome", {})
    print(f"{outcome.get('client_id')}  {outcome.get('name')}")
    print(f"  tier             : {outcome.get('tier')}")
    print(f"  PEP              : {outcome.get('is_pep')}")
    country = outcome.get("sanctions_country")
    print(f"  sanctions country: {country} ({outcome.get('sanctions_basis')})")
    print(f"  adverse media    : {outcome.get('adverse_media_implicating')} implicating")
    for gate, decision in (outcome.get("decisions") or {}).items():
        print(f"  {gate:<17}: {decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "CL-0001")))
