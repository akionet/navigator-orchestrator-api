"""Model layer: provider mapping and the offline fake (SPEC-AIP-002 §3.3, AC-2).

LangChain owns the per-provider adapters, so what is worth testing here is our
half of the contract — the prefix vocabulary, the `Policy` → standard-params
mapping, and the message helpers that keep multimodal content from reaching a
`str` Output contract.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain import chat_models
from langchain_core.messages import AIMessage, HumanMessage

from navigator_orchestrator.engine.llm import (
    PROVIDER_ALIASES,
    FakeChatModel,
    UnknownProviderError,
    make_client,
    text_of,
    usage_of,
)
from navigator_orchestrator.engine.policy import Policy


@pytest.mark.parametrize(
    ("prefix", "langchain_provider"),
    [
        ("vertex", "google_vertexai"),
        ("bedrock", "bedrock_converse"),
        ("anthropic", "anthropic"),
    ],
)
def test_prefix_maps_to_a_langchain_provider(prefix: str, langchain_provider: str) -> None:
    """Our vocabulary is a table row, not a class — that is the point."""
    assert PROVIDER_ALIASES[prefix] == langchain_provider


def test_fake_provider_needs_no_langchain_adapter() -> None:
    assert isinstance(make_client(Policy(model="fake:echo")), FakeChatModel)


def test_unknown_provider_names_the_known_ones() -> None:
    with pytest.raises(UnknownProviderError, match="vertex"):
        make_client(Policy(model="llamafile:whatever"))


def test_a_real_provider_fails_with_an_actionable_missing_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangChain's own ImportError names the package to install, so we don't."""
    policy = Policy(model="vertex:gemini-3.5-pro")
    try:
        make_client(policy)
    except ImportError as exc:
        assert "langchain-google-vertexai" in str(exc)
    except Exception as exc:  # pragma: no cover - only if the extra is installed
        pytest.skip(f"vertex extra appears installed: {type(exc).__name__}")


# ------------------------------------------------------------- message helpers


def test_text_of_plain_string_content() -> None:
    assert text_of(AIMessage(content="pong")) == "pong"


def test_text_of_multimodal_content_keeps_only_text() -> None:
    """Image/tool blocks must not be stringified into a `str` Output contract."""
    message = AIMessage(
        content=[
            {"type": "text", "text": "a menu"},
            {"type": "image_url", "image_url": {"url": "https://example/x.png"}},
            {"type": "text", "text": " for two"},
        ]
    )
    assert text_of(message) == "a menu for two"


def test_text_of_tolerates_bare_strings_and_junk() -> None:
    assert text_of("plain") == "plain"
    assert text_of(AIMessage(content=[])) == ""
    assert text_of(object()) == ""


def test_usage_of_reads_langchains_normalised_shape() -> None:
    message = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 5, "output_tokens": 9, "total_tokens": 14},
    )
    assert usage_of(message) == (5, 9)


def test_usage_of_returns_zeros_when_a_model_reports_nothing() -> None:
    assert usage_of(AIMessage(content="x")) == (0, 0)


# ------------------------------------------------------------------- fake model


async def test_fake_model_echoes_and_counts_calls() -> None:
    model = FakeChatModel()
    message = await model.ainvoke([HumanMessage(content="ping")])
    assert text_of(message) == "ping"
    assert model.calls == 1


async def test_fake_model_reports_usage_so_the_cost_path_is_real() -> None:
    model = FakeChatModel()
    message = await model.ainvoke([HumanMessage(content="ping pong")])
    assert usage_of(message) == (2, 2)


async def test_fake_model_streams_in_chunks() -> None:
    model = FakeChatModel()
    chunks = [c async for c in model.astream([HumanMessage(content="ping")])]
    assert "".join(text_of(c) for c in chunks) == "ping"
    assert len(chunks) > 1


async def test_chunking_varies_by_model_but_text_does_not() -> None:
    """The AC-2 property: a swap is visible in the stream, not in the answer."""
    a = [
        text_of(c)
        async for c in FakeChatModel(model_name="fake:echo").astream([HumanMessage(content="ping")])
    ]
    b = [
        text_of(c)
        async for c in FakeChatModel(model_name="fake:echo-alt").astream(
            [HumanMessage(content="ping")]
        )
    ]
    assert "".join(a) == "".join(b) == "ping"


async def test_fake_model_serves_canned_answers() -> None:
    model = FakeChatModel(canned={"ping": "pong"})
    assert text_of(await model.ainvoke([HumanMessage(content="ping")])) == "pong"


def test_reset_clears_the_call_count() -> None:
    model = FakeChatModel()
    model.calls = 5
    model.reset()
    assert model.calls == 0


# ------------------------------------------------------------ policy → params


def _params(policy: Policy, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what `make_client` would hand LangChain."""
    captured: dict[str, Any] = {}

    def _fake_init(model: str, **kwargs: Any) -> Any:
        captured.update({"model": model, **kwargs})
        return FakeChatModel()

    monkeypatch.setattr(chat_models, "init_chat_model", _fake_init)
    make_client(policy)
    return captured


def test_standard_params_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = Policy(model="vertex:gemini-3.5-pro", max_tokens=2048, timeout_s=30, max_retries=1)
    captured = _params(policy, monkeypatch)
    assert captured["model"] == "gemini-3.5-pro"
    assert captured["model_provider"] == "google_vertexai"
    assert captured["max_tokens"] == 2048
    assert captured["timeout"] == 30
    assert captured["max_retries"] == 1


def test_temperature_is_sent_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini takes it; current Claude models 400 on it. Unset means don't send."""
    assert "temperature" not in _params(Policy(model="bedrock:claude-x"), monkeypatch)
    with_temp = _params(Policy(model="vertex:gemini-3.5-pro", temperature=0.4), monkeypatch)
    assert with_temp["temperature"] == 0.4


def test_effort_is_not_forwarded_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known gap, pinned so it stays deliberate: `effort` has no LangChain
    standard param, so it is carried on `Policy` but not sent. Wiring it needs
    a per-provider adapter and is deferred until a workflow needs it."""
    captured = _params(Policy(model="bedrock:claude-x", effort="max"), monkeypatch)
    assert "effort" not in captured
    assert "output_config" not in captured
