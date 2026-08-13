"""Composition smoke for the browser's hermetic real-HTTP runtime."""

from conftest import running_app

from support.operator_console_app import ECHO_YAML, app


async def test_delivery_runtime_is_provider_free_and_yaml_backed() -> None:
    async with running_app(app) as client:
        discovery = await client.get("/workflows")
        source = await client.get("/workflows/echo/source")
        run = await client.post("/workflows/echo/runs", json={"text": "delivery smoke"})

    workflows = {item["name"]: item for item in discovery.json()["workflows"]}
    assert workflows["echo"]["source_kind"] == "yaml"
    assert workflows["approval"]["checkpointed"] is True
    assert source.text == ECHO_YAML
    assert "event: final" in run.text
