# Setup — running the backend

From a clean machine to a serving runtime. Written against macOS; the only
difference on Linux is the `uv` installer line, and on Windows the shell.

No Docker, no Homebrew, no admin rights. Postgres and Redis are optional extras —
the defaults are in-process, so a laptop with neither is fully supported.

## 1. Install uv

`uv` is the Python package manager this project uses. The standalone installer
puts it in `~/.local/bin` and needs no admin rights and no system Python.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"     # add to ~/.zshrc to persist
uv --version
```

> **Corporate networks.** If your employer does TLS inspection, `uv` may fail to
> reach PyPI until the company root CA is trusted. `export SSL_CERT_FILE=/path/to/corp-ca.pem`
> is usually enough. This is the single most likely thing to block setup.

## 2. Clone

```bash
git clone https://github.com/akionet/navigator-orchestrator-api.git
cd navigator-orchestrator-api
```

## 3. Install dependencies

```bash
uv sync --group dev --extra server
```

This creates `.venv/` and installs Python 3.12 itself if the machine doesn't have
it. `--extra server` adds uvicorn; `--group dev` adds the test and lint tooling.

**You do not need to activate the virtualenv.** Every command below uses
`uv run`, which executes inside `.venv/` automatically. Activating with
`source .venv/bin/activate` is optional convenience if you'd rather type bare
`pytest` and `uvicorn`.

## 4. Configure

```bash
cp .env.example .env
```

**This step is load-bearing, not cosmetic.** `DEFAULT_MODEL` in the code is
`bedrock:anthropic.claude-opus-5` — a real, billable provider. `.env.example`
overrides it with `NAVIGATOR_MODEL=fake:local`, a deterministic offline stub that
makes no network call and costs nothing.

Skip this step and the runtime will either fail on missing AWS credentials mid-demo,
or — if the machine happens to have AWS credentials configured — spend real money
against whatever account they belong to.

`.env` is read automatically at startup by pydantic-settings (`env_file=".env"`).
**Do not source it.** It is application config, not a shell environment or a
virtualenv.

One caveat: `env_file=".env"` resolves relative to the **process's working
directory**, not the repo root. Start the server from the repo root, or the file
is silently ignored.

## 5. Verify before you trust it

```bash
make check
```

Runs ruff, `mypy --strict` and the full suite — 432 tests, about two minutes. It
is exactly what CI runs, reaches no network, and touches no provider. Worth
running in front of a client: it is the fastest honest evidence that the checkout
is healthy.

If `make` is unavailable (no Xcode command line tools), run the same three steps
directly:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
```

## 6. Serve

```bash
uv run --extra server uvicorn navigator_orchestrator.api.app:app --port 8000
```

Or `make run`, which is the same thing with `--reload`.

## 7. Confirm the model before demoing

```bash
curl -s localhost:8000/healthz | jq .engine.model
```

Must print `"fake:local"`. If it prints anything beginning `bedrock:`, `vertex:`
or `anthropic:`, stop — `.env` was not picked up, and the next request will try to
bill someone. Check your working directory.

A healthy response looks like:

```json
{
  "engine": {"state": "ok", "workflows": ["approval", "echo"], "model": "fake:local"},
  "postgres": {"state": "disabled"},
  "redis": {"state": "disabled"}
}
```

`postgres` and `redis` reporting `disabled` is correct and expected — the
in-process stores are the default.

## What you have now

A serving runtime with two reference workflows: `echo` (streams and completes)
and `approval` (pauses at a human gate, and resumes when a decision arrives).

Runs are held **in memory**: restarting the server discards all history. That is
deliberate for the current stage and is called out in the console UI too. The
durable Postgres store is a separate piece of work.

For the workflow demo, see [`kyc_demo.md`](kyc_demo.md).
For the operator console, see the `navigator-orchestrator-app` repository.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `uv: command not found` | `~/.local/bin` not on `PATH` — see step 1 |
| TLS / certificate errors during `uv sync` | corporate TLS inspection; trust the company CA |
| `healthz` shows a `bedrock:` model | `.env` missing, or the server was started from another directory |
| `NotImplementedError: postgres run store` | `NAVIGATOR_RUN_STORE` set to `postgres`; the Postgres store is not implemented yet |
| Port 8000 in use | pass `--port 8001`; note the console's dev proxy expects 8000 |
