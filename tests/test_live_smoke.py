"""The single opt-in live smoke (SPEC-AIP-002 §3.14).

CI never runs this: it is skipped unless `LIVE_LLM=true`, which mirrors the
cloud-e2e opt-in pattern. It exists so the provider wiring has *one* real
exercise before golive, not so the suite depends on a model.
"""

from __future__ import annotations

import os

import pytest

from navigator_orchestrator.config import Settings
from navigator_orchestrator.engine.llm import make_client

pytestmark = pytest.mark.skipif(
    os.getenv("LIVE_LLM", "").lower() != "true",
    reason="live provider smoke; set LIVE_LLM=true to run",
)


@pytest.mark.live
async def test_configured_provider_answers() -> None:  # pragma: no cover - opt-in only
    settings = Settings()
    policy = settings.policy()
    assert policy.provider != "fake", "set NAVIGATOR_MODEL to a real provider for the live smoke"

    client = make_client(policy)
    chunks = [t.text async for t in client.stream([{"role": "user", "content": "Say OK."}], policy)]
    assert "".join(chunks).strip()
