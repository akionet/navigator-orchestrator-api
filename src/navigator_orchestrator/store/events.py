"""The event log (SPEC-EDW-002 §5).

> *"write to new event log table — this should happen throughout, after each
> successful step"*

One entry per step transition, append-only, written **by the runner** rather
than by each step. That placement is the point: a template author cannot forget
to log, and cannot log something that did not happen.

## Backends

`FileEventLog` writes JSONL under `runs/<run_id>/events.jsonl`. Append-only
falls out of opening in append mode — nothing in the code path can rewrite an
earlier line.

The Postgres backend arrives with the platform split and carries the same
columns, where append-only becomes a **grant** rather than a property of how
the file happens to be opened. That is the stronger guarantee and the reason
the schema below is fixed now: an audit trail the application can rewrite is
not an audit trail.

## `started` and terminal rows

`SPEC-EDW-002` §5 says a row means the step succeeded. That was imprecise, and
this module is the correction: a step emits **`started`, then exactly one
terminal row** (`ok` | `skipped` | `failed` | `blocked`).

Both are wanted. A `started` with no terminal row is how a hung or killed step
looks, and "what is this run waiting for" is the first question anyone asks —
losing that to keep a tidier invariant would trade the answer for the aesthetic.
It is `ok` specifically, not the presence of a row, that means success.

## `detail` is a summary

Never the payload. Full drafts belong in the run store; copying them into every
row makes the table unqueryable, duplicates personal data, and turns a log you
can read into one you have to grep.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

__all__ = [
    "EventLog",
    "EventStatus",
    "FileEventLog",
    "InMemoryEventLog",
    "NullEventLog",
    "StepEvent",
]

EventStatus = Literal[
    "started",
    "ok",
    "skipped",
    "failed",
    "blocked",
    "awaiting_human",
    # A paused run continued. Distinct from `started`: it names the moment a
    # human decision re-entered the run, which is what an audit reads for.
    "resumed",
]

#: Statuses that end a step. Exactly one of these follows every `started`.
TERMINAL: frozenset[str] = frozenset({"ok", "skipped", "failed", "blocked"})


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One row. Field names are the eventual column names."""

    run_id: str
    seq: int
    workflow: str
    step: str
    status: EventStatus
    actor: str = "system"
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["at"] = self.at or datetime.now(UTC).isoformat(timespec="seconds")
        return row


class EventLog(Protocol):
    """Append one entry. Implementations must never raise into a run."""

    def append(self, event: StepEvent) -> None: ...

    def read(self, run_id: str) -> list[dict[str, Any]]: ...


class NullEventLog:
    """Records nothing. The default, so `run_template` stays usable in a test
    without a filesystem."""

    def append(self, event: StepEvent) -> None:
        return None

    def read(self, run_id: str) -> list[dict[str, Any]]:
        return []


@dataclass(slots=True)
class InMemoryEventLog:
    """For tests, and for asserting on what a run recorded."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, event: StepEvent) -> None:
        self.entries.append(event.as_row())

    def read(self, run_id: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["run_id"] == run_id]


@dataclass(slots=True)
class FileEventLog:
    """JSONL under `root/<run_id>/events.jsonl`.

    Append-only by construction: opened with `"a"`, never `"w"`, and nothing
    here seeks or truncates.
    """

    root: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def path_for(self, run_id: str) -> Path:
        return self.root / run_id / "events.jsonl"

    def append(self, event: StepEvent) -> None:
        # Telemetry must never take down the thing it observes. A log that can
        # fail a run is worse than no log.
        try:
            target = self.path_for(event.run_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.as_row(), default=str, sort_keys=True)
            with self._lock, target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:  # noqa: S110 - deliberately total; see the comment above
            pass

    def read(self, run_id: str) -> list[dict[str, Any]]:
        target = self.path_for(run_id)
        if not target.is_file():
            return []
        out: list[dict[str, Any]] = []
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A truncated final line is what a killed process leaves. Skip
                # it rather than refusing to show the run at all.
                continue
        return out

    def run_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "events.jsonl").is_file())
