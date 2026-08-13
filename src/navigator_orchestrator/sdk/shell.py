"""The `shell` executor (SPEC-NSP-005 §6).

> *"A workflow CLI that can be configured to work with any backend api **and/or
> shell script** (think localhost)"*

The step vocabulary had no way to run a command, so editorial work that ends in
`aws s3 cp` or `make deploy` had nowhere to go. This is that step.

A shell step is the widest hole in any workflow engine, so it is narrow here on
purpose:

1. **The command comes from the template, never from the pool.** Interpolating a
   model's output — or a fetched record title — into a command line is remote
   code execution with extra steps. Pool values reach the process as
   *environment variables* instead, where they are data no matter what they
   contain.
2. **No shell interpretation.** An argument list, never a string, and never
   `shell=True`. `rm -rf $DIR` with an empty `DIR` is a shell feature, not a
   command.
3. **Non-zero exit fails the step**, with the exit code and captured output in
   the event detail. Silence on failure is the outcome that produces "the
   workflow said it published it" a week later.
4. **The timeout is mandatory** and defaulted, never unbounded. A hung command
   should fail a run, not hold a checkpoint open forever.

Point 1 is what makes the rest work. Given that, an argument list, and no shell,
the remaining attack surface is what the template author wrote — which is code
review's job, and code review can see it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

from navigator_orchestrator.sdk.context import Ctx
from navigator_orchestrator.sdk.templates import Step

__all__ = ["ShellFailed", "ShellResult", "run_shell_step"]

#: How much of stdout/stderr is kept. The event log is a summary; a command that
#: prints a megabyte should not make the run's history unreadable.
CAPTURE_LIMIT = 4000

#: Pool values are passed as environment variables. Only scalars: a dict has no
#: single obvious rendering, and inventing one would be a serialisation format
#: nobody asked for.
ENV_PREFIX = "NAV_"


class ShellFailed(RuntimeError):
    """The command exited non-zero, timed out, or could not be started."""


class ShellResult(dict[str, Any]):
    """What a `shell` step produces: `{"exit": 0, "stdout": ..., "stderr": ...}`.

    A plain dict subclass so it lands in the pool as ordinary data — a
    checkpointer has to serialise this, and an object with behaviour would not
    survive a pause.
    """


def env_for(pool: Mapping[str, Any], allowed: tuple[str, ...]) -> dict[str, str]:
    """The environment additions for a step, from its declared `kwargs`.

    Scoped by `kwargs` like every other step's inputs, so what a command can see
    is declared in the template and visible in one place.
    """
    extra: dict[str, str] = {}
    for key in allowed:
        value = pool.get(key)
        if isinstance(value, (str, int, float, bool)):
            extra[f"{ENV_PREFIX}{key.upper()}"] = str(value)
    return extra


async def run_shell_step(step: Step, ctx: Ctx, pool: Mapping[str, Any]) -> ShellResult:
    """Run `step.command`, returning exit code and captured output.

    Raises `ShellFailed` on a non-zero exit or a timeout, so the runner records
    it exactly as it records any other failing step.
    """
    command = list(step.command)
    environment = {**os.environ, **env_for(pool, step.kwargs)}

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ctx.files.root),
            env=environment,
        )
    except OSError as exc:
        # A missing binary is the common case and deserves to say so, rather
        # than surfacing as a bare FileNotFoundError three frames up.
        raise ShellFailed(f"cannot run {command[0]!r}: {exc}") from exc

    try:
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=step.timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ShellFailed(
            f"{command[0]!r} exceeded its {step.timeout}s timeout and was killed"
        ) from None

    stdout = _text(raw_out)
    stderr = _text(raw_err)
    exit_code = process.returncode or 0

    if exit_code != 0:
        raise ShellFailed(
            f"{command[0]!r} exited {exit_code}: {(stderr or stdout).strip()[:500] or 'no output'}"
        )
    return ShellResult(exit=exit_code, stdout=stdout, stderr=stderr)


def _text(raw: bytes | None) -> str:
    """Decode captured output.

    `errors="replace"`: a command that emits one invalid byte should not take
    down the run that called it, and on Windows the output encoding is whatever
    the child process felt like.
    """
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")[:CAPTURE_LIMIT]
