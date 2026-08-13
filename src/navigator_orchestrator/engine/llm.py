"""Model access via LangChain chat models (SPEC-AIP-002 §3.3, C-4, AC-2).

Nodes never construct a client. They receive a `BaseChatModel` on `deps.llm`
and call it; switching providers is a `Policy.model` change with zero node
edits — that is what AC-2 asserts, and what makes the GCP-UAT → AWS-golive
port a config change rather than a rewrite.

| `Policy.model` prefix | LangChain provider  | Serves                   | Used for     |
|-----------------------|---------------------|--------------------------|--------------|
| `vertex:`             | `google_vertexai`   | Gemini on Vertex AI      | UAT (GCP)    |
| `bedrock:`            | `bedrock_converse`  | Claude on Amazon Bedrock | golive (AWS) |
| `anthropic:`          | `anthropic`         | Claude, first-party API  | escape hatch |
| `fake:`               | —                   | deterministic, offline   | every test   |

LangChain owns the per-provider adapters, request translation and usage
normalisation. We own the prefix vocabulary and the `Policy` → standard-params
mapping, and nothing else — deliberately, so a new provider is a table row
rather than a class. Provider packages stay optional extras; `init_chat_model`
raises an actionable ImportError naming the missing one.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from navigator_orchestrator.engine.policy import Policy

__all__ = [
    "PROVIDER_ALIASES",
    "FakeChatModel",
    "UnknownProviderError",
    "make_client",
    "text_of",
    "usage_of",
]

#: Our prefix → LangChain's provider id. `bedrock_converse` is the Converse-API
#: client, which is the one that reports usage consistently.
PROVIDER_ALIASES: dict[str, str] = {
    "vertex": "google_vertexai",
    "bedrock": "bedrock_converse",
    "anthropic": "anthropic",
}


class UnknownProviderError(ValueError):
    """`Policy.model` named a provider outside `PROVIDER_ALIASES`."""


def make_client(policy: Policy) -> BaseChatModel:
    """Build the chat model for `policy`. The *only* place providers are named.

    Only LangChain's **standard params** are passed, so the same mapping works
    for every provider: a provider-specific knob belongs behind an adapter,
    not in this function.
    """
    if policy.provider == "fake":
        return FakeChatModel(model_name=policy.model)

    try:
        provider = PROVIDER_ALIASES[policy.provider]
    except KeyError as exc:
        known = ", ".join(sorted([*PROVIDER_ALIASES, "fake"]))
        raise UnknownProviderError(
            f"unknown provider {policy.provider!r} in {policy.model!r}; known: {known}"
        ) from exc

    params: dict[str, Any] = {
        "max_tokens": policy.max_tokens,
        "timeout": policy.timeout_s,
        "max_retries": policy.max_retries,
    }
    # Provider-dependent: Gemini accepts temperature, current Claude models
    # reject it with a 400. Unset means "don't send", which is right for both.
    if policy.temperature is not None:
        params["temperature"] = policy.temperature

    from langchain.chat_models import init_chat_model  # noqa: PLC0415 - import cost

    model: BaseChatModel = init_chat_model(policy.model_id, model_provider=provider, **params)
    return model


# --------------------------------------------------------------- message helpers


def text_of(message: Any) -> str:
    """Flatten message content to text, tolerating multimodal blocks.

    Chat models may return a plain string or a list of content blocks. Only the
    text parts are meaningful to a `str`-typed Output contract; images and tool
    blocks are dropped here rather than stringified into nonsense.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def usage_of(message: Any) -> tuple[int, int]:
    """`(input_tokens, output_tokens)` from LangChain's normalised usage.

    Providers report usage differently; `usage_metadata` is the shape LangChain
    normalises them into, which is most of why this layer is worth having.
    Returns zeros when a model reports nothing rather than guessing.
    """
    metadata = getattr(message, "usage_metadata", None) or {}
    if not isinstance(metadata, dict):  # pragma: no cover - defensive
        return 0, 0
    return int(metadata.get("input_tokens", 0)), int(metadata.get("output_tokens", 0))


# ------------------------------------------------------------------- fake client


class FakeChatModel(BaseChatModel):
    """Deterministic offline chat model backing every test ($0 tokens).

    Echoes the last human message, streams it in chunks, and reports usage so
    the cost-meter path (AC-5) is exercised rather than stubbed. Chunk size
    varies with the model name so a provider swap is observable in the stream
    while the completed text stays identical — the property AC-2 asserts.
    """

    model_name: str = "fake:echo"
    canned: dict[str, str] = {}  # noqa: RUF012 - pydantic field, not a class constant
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _answer(self, messages: list[Any]) -> str:
        prompt = text_of(messages[-1]) if messages else ""
        return self.canned.get(prompt, prompt)

    def _chunk_size(self) -> int:
        import hashlib  # noqa: PLC0415 - trivial, keeps module import light

        return 1 + hashlib.sha256(self.model_name.encode()).digest()[0] % 3

    def _result(self, text: str, messages: list[Any]) -> Any:
        from langchain_core.messages import AIMessage  # noqa: PLC0415
        from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: PLC0415

        message = AIMessage(
            content=text,
            usage_metadata={
                "input_tokens": sum(len(text_of(m).split()) for m in messages),
                "output_tokens": max(1, len(text.split())),
                "total_tokens": 0,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # `run_manager` is passed positionally by LangChain, so it is named here
    # rather than swallowed by **kwargs.
    def _generate(
        self,
        messages: list[Any],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.calls += 1
        return self._result(self._answer(messages), messages)

    def _stream(
        self,
        messages: list[Any],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        from langchain_core.messages import AIMessageChunk  # noqa: PLC0415
        from langchain_core.outputs import ChatGenerationChunk  # noqa: PLC0415

        self.calls += 1
        text = self._answer(messages)
        size = self._chunk_size()
        chunks = [text[i : i + size] for i in range(0, len(text), size)] or [""]
        for index, chunk in enumerate(chunks):
            usage = None
            if index == len(chunks) - 1:
                usage = {
                    "input_tokens": sum(len(text_of(m).split()) for m in messages),
                    "output_tokens": max(1, len(text.split())),
                    "total_tokens": 0,
                }
            yield ChatGenerationChunk(message=AIMessageChunk(content=chunk, usage_metadata=usage))

    def reset(self) -> None:
        self.calls = 0
