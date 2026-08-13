"""Building the dependencies a run needs — the composition root (SPEC-AIP-002 §3).

One place names providers and builds clients. `ruff.toml` bans
`engine.llm.make_client` everywhere else, because a step that builds its own
client is a step whose model cannot be swapped, mocked, or attributed.

This module exists because that ban had no legitimate escape for a **workflow
project**. `editorial/` could author flows but could not write a script that
ran one piece of one — evaluating a judge against a fixed test set, say — with
a real model. The options were to weaken the rule where it matters most, or to
export the root. Exporting it is the honest one.
"""

from __future__ import annotations

import os
from pathlib import Path

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.prompts import PromptRegistry

__all__ = ["build_deps"]


def build_deps(model: str = "", *, prompts_dir: Path | None = None) -> Deps:
    """Dependencies for a run, with a real client only when one is named.

    `model` defaults to `NAVIGATOR_MODEL`. Unset — or anything starting
    `fake:` — yields the hermetic `FakeChatModel`, so a workflow costs nothing
    until it is explicitly asked to spend.
    """
    resolved = model or os.environ.get("NAVIGATOR_MODEL") or ""
    directory = prompts_dir or Path(
        os.environ.get("NAVIGATOR_PROMPTS_DIR") or Path(__file__).resolve().parents[3] / "prompts"
    )
    deps = Deps(prompts=PromptRegistry.from_dir(directory) if directory.is_dir() else None)
    if not resolved or resolved.startswith("fake:"):
        return deps

    # Imported here, not at module scope: provider adapters are optional extras
    # and the hermetic path must not require one to be installed.
    from navigator_orchestrator.engine.llm import make_client  # noqa: PLC0415
    from navigator_orchestrator.engine.policy import with_overrides  # noqa: PLC0415

    policy = with_overrides(deps.policy, model=resolved)
    return deps.with_(llm=make_client(policy), policy=policy)
