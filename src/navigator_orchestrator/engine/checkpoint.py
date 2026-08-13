"""LangGraph checkpointer wiring (SPEC-AIP-002 §3.6).

Opt-in per graph. `echo` runs checkpointer-off — the house rule is not to
over-checkpoint short-lived graphs — but the seam and a resumable smoke exist
now so ATT's HITL graphs (R1) inherit them rather than inventing them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

__all__ = ["CheckpointerKind", "checkpointer_scope", "make_memory_checkpointer"]

#: One seam, four backends (SPEC-NSP-003 §2.2). `sqlite` is the CLI default —
#: durable with no server — and `postgres` is what an enterprise deployment
#: swaps in. Both satisfy `BaseCheckpointSaver`, so switching is a config value
#: and never a code change.
CheckpointerKind = Literal["none", "memory", "sqlite", "postgres"]


def make_memory_checkpointer() -> Any:
    """In-process saver — proves resumability without a database."""
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: PLC0415 - lazy by design

    return InMemorySaver()


@asynccontextmanager
async def checkpointer_scope(
    kind: CheckpointerKind,
    dsn: str | None = None,
) -> AsyncIterator[Any | None]:
    """Yield a checkpointer (or `None`) for the lifetime of the app.

    Postgres is a scoped resource — hence a context manager rather than a
    plain factory, so connections close on shutdown.
    """
    if kind == "none":
        yield None
        return
    if kind == "memory":
        yield make_memory_checkpointer()
        return

    if kind == "sqlite":
        # Durable, no server, one file. `dsn` is a path here rather than a URL;
        # ":memory:" is accepted so a test can exercise the same code path
        # without touching disk.
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415

        path = dsn or ".navigator-orchestrator/checkpoints.sqlite"
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            yield saver
        return

    if not dsn:
        raise ValueError("postgres checkpointer needs NAVIGATOR_DATABASE_URL")
    try:
        from langgraph.checkpoint.postgres.aio import (  # noqa: PLC0415 - optional extra
            AsyncPostgresSaver,
        )
    except ImportError as exc:  # pragma: no cover - depends on install profile
        raise RuntimeError(
            "postgres checkpointer needs the optional extra: `uv sync --extra postgres`"
        ) from exc

    # pragma: no cover below - needs a reachable database (CI service container)
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:  # pragma: no cover
        await saver.setup()
        yield saver
