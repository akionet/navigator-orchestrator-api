"""One typed config surface (SPEC-AIP-002 §3.1).

Env → `Settings` → `Policy`/clients at the composition root. Nothing deeper in
the stack reads the environment, which is what makes the GCP-UAT → AWS-golive
port a config change rather than a rewrite (SPEC-AIP-004).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from navigator_orchestrator.engine.checkpoint import CheckpointerKind
from navigator_orchestrator.engine.policy import DEFAULT_MODEL, Effort, Policy

__all__ = ["Settings", "default_prompts_dir", "get_settings"]


def default_prompts_dir() -> Path:
    """Repo-root `prompts/` when running from a checkout, else `./prompts`."""
    candidate = Path(__file__).resolve().parents[2] / "prompts"
    return candidate if candidate.is_dir() else Path("prompts")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NAVIGATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "navigator-orchestrator-api"

    # --- model -------------------------------------------------------------
    #: `<provider>:<model-id>`. UAT sets this to a `vertex:` Gemini model;
    #: golive moves it to `bedrock:` Claude with no other change.
    model: str = DEFAULT_MODEL
    effort: Effort | None = None
    max_tokens: int = Field(default=4096, gt=0)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    #: Optional allowlist for the client-settable `?model=` override. Empty
    #: (the default) accepts any model — convenient for UAT comparison runs,
    #: worth pinning before anything cost-sensitive is exposed.
    allowed_models: tuple[str, ...] = ()

    # --- providers ---------------------------------------------------------
    aws_region: str = "us-east-1"
    #: Vertex AI project/location. Required when `model` starts with `vertex:`.
    gcp_project: str | None = None
    gcp_region: str = "global"

    # --- runs & human-in-the-loop (SPEC-AIP-003) --------------------------
    #: Durable run/decision records. `memory` is per-process and fine for dev;
    #: `postgres` is required for a resume to survive a restart.
    run_store: Literal["memory", "postgres"] = "memory"
    #: Abandoned `awaiting_decision` runs become `cancelled` after this long.
    #: Decision chains are kept regardless — an abandoned run is not a deleted
    #: audit record.
    run_ttl_days: int = Field(default=5, ge=1)
    #: Reject a decision that cannot be attributed to a principal. Off in dev,
    #: on everywhere an audit trail is claimed.
    require_principal: bool = False
    #: Identity header, set by a trusted upstream proxy (GCP IAP on Cloud Run).
    principal_header: str = "x-goog-iap-jwt-assertion"

    # --- stores ------------------------------------------------------------
    redis_url: str | None = None
    database_url: str | None = None
    checkpointer: CheckpointerKind = "none"
    cache_enabled: bool = True

    # --- prompts -----------------------------------------------------------
    prompts_dir: Path = Field(default_factory=default_prompts_dir)

    # --- observability -----------------------------------------------------
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    #: Opt-in for the single `@live` smoke; CI never sets it.
    live_llm: bool = False

    def policy(self) -> Policy:
        return Policy(
            model=self.model,
            effort=self.effort,
            max_tokens=self.max_tokens,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
