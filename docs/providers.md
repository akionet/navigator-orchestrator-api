# Model providers

The engine is provider-agnostic by construction: **nodes never build a client.**
They receive a `BaseChatModel` on `deps.llm` and call it. Swapping providers is a
configuration change, and `engine/llm.py::make_client` is the only place in the
codebase where a provider is named.

There are three seams, in increasing order of power. Use the weakest one that
works.

## Seam 1 — configuration only

If your provider is already in the alias table, this is the whole job:

```bash
NAVIGATOR_MODEL=anthropic:claude-sonnet-4-6
```

The format is always `<provider>:<model-id>`. Everything after the first colon is
passed through untouched, so vendor-prefixed ids (`anthropic.claude-...` on
Bedrock) work without special handling.

| Prefix | LangChain provider | Extra to install |
|---|---|---|
| `fake` | — | none — **the default** |
| `anthropic` | `anthropic` | `--extra anthropic` |
| `bedrock` | `bedrock_converse` | `--extra bedrock` |
| `vertex` | `google_vertexai` | `--extra vertex` |

`fake:` is the built-in default on purpose: a fresh clone cannot reach a paid
provider until someone chooses one. `tests/test_policy.py` asserts this, so it
cannot be "temporarily" changed without a failing build.

Per-request overrides are also supported (`?model=`), optionally restricted with
`NAVIGATOR_ALLOWED_MODELS`. Pin that allowlist before exposing the API to anyone.

## Seam 2 — adding a provider LangChain already supports

One row in `PROVIDER_ALIASES` in `engine/llm.py`, plus an optional dependency:

```python
PROVIDER_ALIASES: dict[str, str] = {
    "vertex": "google_vertexai",
    "bedrock": "bedrock_converse",
    "anthropic": "anthropic",
    "azure": "azure_openai",      # ← added
}
```

LangChain owns the adapter, request translation and usage normalisation. We own
the prefix vocabulary and the `Policy` → standard-params mapping, and nothing
else. `init_chat_model` raises an actionable `ImportError` naming the missing
package if the extra isn't installed.

Anything LangChain has a chat-model integration for — Azure OpenAI, watsonx,
Mistral, Ollama, or an OpenAI-compatible endpoint — is a one-line change here.

## Seam 3 — a client the alias table cannot express

Client certificates, mutual TLS, a private gateway, a bespoke auth header, a
model served inside your own network: none of these are a *provider prefix*
problem. They are client-construction problems, and the seam for them is
`client_factory`.

`build_context` accepts a callable that receives the resolved `Policy` and
returns any `BaseChatModel`. It replaces `make_client` entirely, so the engine
never needs to know how the client was built:

```python
from langchain_core.language_models import BaseChatModel
from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.engine.policy import Policy


def corporate_client(policy: Policy) -> BaseChatModel:
    """Client certificates and a private endpoint — invisible to every node."""
    import httpx
    from langchain_openai import ChatOpenAI     # OpenAI-compatible gateway

    return ChatOpenAI(
        model=policy.model_id,
        base_url="https://llm-gateway.internal.example",
        api_key="unused",                        # gateway authenticates by cert
        http_client=httpx.Client(
            cert=("/etc/pki/client.pem", "/etc/pki/client.key"),
            verify="/etc/pki/corporate-ca.pem",
        ),
        max_tokens=policy.max_tokens,
        timeout=policy.timeout_s,
        max_retries=policy.max_retries,
    )


app = build_app(client_factory=corporate_client)
```

Serve that module instead of the default entry point:

```bash
uv run --extra server uvicorn yourorg.app:app --port 8000
```

Three things make this safe:

- **It sees each request's `Policy`**, so a per-request `?model=` override still
  rebuilds the right client rather than being silently ignored.
- **Nothing else changes.** No node, workflow, prompt or test is aware of it —
  `tests/test_purity.py` enforces that nodes cannot construct clients even if
  someone tries.
- **No credential reaches the browser.** The console is served same-origin behind
  the runtime; certificates and keys stay on the server side.

Keep certificate paths in environment variables rather than literals, and keep
the factory in your own module rather than patching `llm.py` — that way you
inherit upstream changes without a merge conflict on every pull.

## What is genuinely portable, and what is not

Portable, because LangChain normalises it: message roles, streaming, token usage
(`usage_metadata`), and tool-call shape.

Not portable, and handled explicitly:

- **Temperature.** Gemini accepts it; current Claude models reject it with a 400.
  `Policy.temperature` defaults to unset, which means "don't send" — correct for
  both. Set it only when you know the target accepts it.
- **Cost.** `estimate_cost` carries a rate table keyed by model id. An unknown
  model meters tokens with `cost_usd = None` rather than guessing — a missing
  rate shows as absent, never as zero.
- **Model ids.** No attempt is made to translate them between providers. `vertex:`
  and `bedrock:` name genuinely different models; pretending otherwise would hide
  a real change behind a config key.

## Verifying a provider swap

```bash
curl -s localhost:8000/healthz | jq .engine.model    # what is actually loaded
```

Then run the `echo` workflow and confirm the stream completes. `echo` calls the
model and returns its text, so it is the shortest end-to-end proof that
credentials, network path and adapter all work — without involving a workflow's
own logic.

The one opt-in live smoke test is `make test-live`, which requires `LIVE_LLM=true`.
Everything else in the suite is hermetic and reaches no provider.
