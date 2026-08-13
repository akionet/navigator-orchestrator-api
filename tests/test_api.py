"""HTTP surface (SPEC-AIP-002 §3.8, TODO-6, AC-7)."""

from __future__ import annotations

from typing import Any

from conftest import parse_sse, running_app
from fastapi import FastAPI

from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.engine.llm import FakeChatModel


async def test_healthz_reports_every_dependency(app: FastAPI) -> None:
    async with running_app(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["engine"]["state"] == "ok"
    assert body["engine"]["workflows"] == ["approval", "echo"]
    # Nothing configured in the hermetic profile → disabled, not unavailable.
    assert body["postgres"]["state"] == "disabled"
    assert body["redis"]["state"] == "disabled"


async def test_run_streams_sse_events(app: FastAPI) -> None:
    async with running_app(app) as client:
        response = await client.post("/workflows/echo/runs", json={"text": "ping"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)

    kinds = [e["event"] for e in events]
    assert "token" in kinds
    assert kinds[0] == "node"
    final = next(e for e in events if e["event"] == "final")
    assert final["data"]["output"]["text"] == "ping"


async def test_unknown_workflow_is_404(app: FastAPI) -> None:
    async with running_app(app) as client:
        response = await client.post("/workflows/nope/runs", json={"text": "ping"})
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_workflow"


async def test_invalid_input_is_422_before_any_model_call(app: FastAPI, fake_llm: Any) -> None:
    async with running_app(app) as client:
        response = await client.post("/workflows/echo/runs", json={"wrong": 1})
    assert response.status_code == 422
    assert response.json()["direction"] == "input"
    assert fake_llm.calls == 0


async def test_malformed_json_is_400(app: FastAPI) -> None:
    async with running_app(app) as client:
        response = await client.post(
            "/workflows/echo/runs",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


async def test_authz_stub_is_wired_into_the_route(app: FastAPI) -> None:
    """The seam exists at R0 so real auth is not retrofitted later."""
    async with running_app(app) as client:
        response = await client.post("/workflows/echo/runs", json={"text": "ping"})
    assert response.headers["x-navigator-orchestrator-principal"] == "anonymous"


async def test_model_can_be_overridden_per_request(app: FastAPI) -> None:
    """A client picks the model per call — the UAT comparison lever."""
    async with running_app(app) as client:
        response = await client.post(
            "/workflows/echo/runs?model=fake:echo-alt", json={"text": "ping"}
        )
    final = next(e for e in parse_sse(response.text) if e["event"] == "final")
    assert final["data"]["output"]["model"] == "fake:echo-alt"
    assert final["data"]["output"]["text"] == "ping"


def _recording_app(settings: Any, cache: Any, observability: Any) -> tuple[FastAPI, list[Any]]:
    """An app whose client factory records the `Policy` each run resolved to."""
    seen: list[Any] = []

    def factory(policy: Any) -> FakeChatModel:
        seen.append(policy)
        return FakeChatModel(model_name=policy.model)

    app = build_app(settings, client_factory=factory, cache=cache, observability=observability)
    return app, seen


async def test_temperature_can_be_overridden_per_request(
    settings: Any, cache: Any, observability: Any
) -> None:
    app, seen = _recording_app(settings, cache, observability)
    async with running_app(app) as client:
        await client.post("/workflows/echo/runs?temperature=0.7", json={"text": "ping"})
    assert seen[-1].temperature == 0.7


async def test_omitting_overrides_uses_the_deployment_policy(
    settings: Any, cache: Any, observability: Any
) -> None:
    app, seen = _recording_app(settings, cache, observability)
    async with running_app(app) as client:
        await client.post("/workflows/echo/runs", json={"text": "ping"})
    assert seen[-1].model == "fake:echo"
    assert seen[-1].temperature is None


async def test_a_malformed_model_override_is_400_not_500(app: FastAPI) -> None:
    async with running_app(app) as client:
        response = await client.post(
            "/workflows/echo/runs?model=gemini-3.5-pro", json={"text": "ping"}
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_policy"


async def test_out_of_range_temperature_is_rejected_by_the_route(app: FastAPI) -> None:
    async with running_app(app) as client:
        response = await client.post("/workflows/echo/runs?temperature=5", json={"text": "ping"})
    assert response.status_code == 422  # FastAPI query validation


async def test_allowlist_blocks_an_unlisted_model(
    settings: Any, fake_llm: Any, cache: Any, observability: Any
) -> None:
    """Deployments that care about cost can pin the set clients may choose."""
    pinned = settings.model_copy(update={"allowed_models": ("fake:echo",)})
    app = build_app(pinned, llm=fake_llm, cache=cache, observability=observability)
    async with running_app(app) as client:
        blocked = await client.post(
            "/workflows/echo/runs?model=fake:echo-alt", json={"text": "ping"}
        )
        allowed = await client.post("/workflows/echo/runs?model=fake:echo", json={"text": "ping"})
    assert blocked.status_code == 403
    assert blocked.json()["error"] == "model_not_allowed"
    assert allowed.status_code == 200


async def test_repeated_request_is_served_from_cache(app: FastAPI, fake_llm: Any) -> None:
    async with running_app(app) as client:
        await client.post("/workflows/echo/runs", json={"text": "ping"})
        second = await client.post("/workflows/echo/runs", json={"text": "ping"})
    final = next(e for e in parse_sse(second.text) if e["event"] == "final")
    assert final["data"]["cached"] is True
    assert fake_llm.calls == 1
