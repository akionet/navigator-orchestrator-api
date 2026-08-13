"""`Policy` — the one config surface for a run (SPEC-AIP-002 §3.2, C-4).

Swapping models is a `Policy.model` change and nothing else (AC-2): the string
is `"<provider>:<model-id>"` and the factory in `llm.py` dispatches on the
prefix. Nodes never read this file's defaults; the composition root supplies a
Policy built from `config.Settings`.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["DEFAULT_MODEL", "Effort", "Policy", "with_overrides"]

# The offline stub is the default, deliberately. A fresh clone that someone runs
# before reading anything must not be able to spend money or require a cloud
# account — reaching a paid provider is an explicit choice, made by setting
# `NAVIGATOR_MODEL` (see `.env.example` and `docs/providers.md`).
#
# This is the *only* line in the engine that expresses a provider preference.
# Everything else is a `<provider>:<model-id>` string supplied by config.
DEFAULT_MODEL = "fake:local"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Policy(BaseModel):
    """Model, budget and reliability knobs for one run.

    Frozen so it can be part of a cache key and shared across nodes without
    anyone mutating it mid-run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = DEFAULT_MODEL
    # Provider-dependent, which is why it defaults to unset rather than to a
    # number: Gemini accepts `temperature` (0 to 2, the wider of the two
    # ranges), while current Claude models (Opus 5, Sonnet 5, Opus 4.8/4.7)
    # reject it with a 400. Clients set it for UAT/Gemini; on Claude it is left
    # alone and depth comes from `effort` instead.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    #: Anthropic-only. `GeminiClient` ignores it deliberately — see its docstring.
    effort: Effort | None = None
    max_retries: int = Field(default=2, ge=0, le=10)
    timeout_s: float = Field(default=60.0, gt=0)
    budget_tokens: int | None = Field(default=None, gt=0)
    max_tokens: int = Field(default=4096, gt=0)

    @field_validator("model")
    @classmethod
    def _require_provider_prefix(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError(
                "model must be '<provider>:<model-id>', e.g. 'bedrock:anthropic.claude-opus-5'"
            )
        return value

    @property
    def provider(self) -> str:
        """The factory key — everything before the first colon."""
        return self.model.split(":", 1)[0]

    @property
    def model_id(self) -> str:
        """The provider-native model id — everything after the first colon."""
        return self.model.split(":", 1)[1]

    def fingerprint(self) -> str:
        """Stable serialisation for cache keys (SPEC-AIP-002 §3.6)."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def with_overrides(
    base: Policy,
    *,
    model: str | None = None,
    temperature: float | None = None,
) -> Policy:
    """Apply per-request overrides to the deployment's default policy.

    Rebuilds through the constructor rather than `model_copy(update=...)`,
    which skips validators — a client-supplied `model` must still be checked
    for its provider prefix, and a client-supplied `temperature` for range.
    """
    data = base.model_dump()
    if model is not None:
        data["model"] = model
    if temperature is not None:
        data["temperature"] = temperature
    return Policy(**data)
