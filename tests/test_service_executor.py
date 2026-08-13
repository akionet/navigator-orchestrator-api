"""B2-B5 - the service executor against a transport (SPEC-NSP-006).

`httpx.MockTransport` rather than a mock of our own: it hands the test the
actual `httpx.Request` the client would put on the wire, so an assertion about
the URL is an assertion about the URL — not about a mock agreeing with the code
that configured it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.project import Backend, Project
from navigator_orchestrator.sdk.runner import StepFailed, run_template
from navigator_orchestrator.sdk.service import (
    Call,
    CallSpecError,
    ServiceFailed,
    backend_requirements,
    bearer,
    resolve_backend,
    run_service_step,
)
from navigator_orchestrator.sdk.templates import Step, Template
from navigator_orchestrator.store.events import InMemoryEventLog

# A fixture value. The point of it is that it must never appear in an event
# row, and a test that scans for a secret has to hold one.
TOKEN = "s3cret-token-value-nobody-should-see"  # noqa: S105


@pytest.fixture
def deps() -> Deps:
    return Deps(prompts=None, llm=FakeChatModel(model_name="fake:echo"))


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Backend:
    monkeypatch.setenv("SERVICE_TOKEN", TOKEN)
    monkeypatch.delenv("FALLBACK_TOKEN", raising=False)
    return Backend(
        name="client-service",
        base_url="https://api.example.com",
        token_env=("SERVICE_TOKEN", "FALLBACK_TOKEN"),
        timeout=5.0,
    )


def ctx_for(tmp_path: Path, deps: Deps, project: Any = None, **params: Any) -> Ctx:
    return Ctx(params=params, deps=deps, files=FileAccess(root=tmp_path), project=project)


def transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_ok(payload: Any, status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen.append(request)  # type: ignore[attr-defined]
        return httpx.Response(status, json=payload)

    handler.seen = []  # type: ignore[attr-defined]
    return handler


# ── B2: it performs the call ─────────────────────────────────────────────────


async def test_a_get_returns_the_parsed_body(tmp_path: Path, deps: Deps, backend: Backend) -> None:
    handler = json_ok([{"_id": "RH8FMGQ-WF", "_status": "PENDING"}])
    step = Step(
        "select",
        "service",
        produces="pending",
        kwargs=("status",),
        backend="client-service",
        call=Call("GET", "/v1/workflows", query={"status": "$status"}),
    )
    async with transport(handler) as client:
        produced = await run_service_step(
            step, backend, ctx_for(tmp_path, deps), {"status": "PENDING"}, client=client
        )

    assert produced == [{"_id": "RH8FMGQ-WF", "_status": "PENDING"}]
    sent = handler.seen[0]
    assert sent.method == "GET"
    assert str(sent.url) == "https://api.example.com/v1/workflows?status=PENDING"


async def test_a_post_sends_the_body(tmp_path: Path, deps: Deps, backend: Backend) -> None:
    handler = json_ok({"inserted": 1})
    step = Step(
        "store",
        "service",
        produces="stored",
        kwargs=("request_id",),
        backend="client-service",
        call=Call("POST", "/v1/submission", body={"requestId": "$request_id"}),
    )
    async with transport(handler) as client:
        await run_service_step(
            step, backend, ctx_for(tmp_path, deps), {"request_id": "RQ1"}, client=client
        )

    assert json.loads(handler.seen[0].content) == {"requestId": "RQ1"}


async def test_a_path_value_reaches_the_wire_encoded(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """The B1 property, asserted on the actual request rather than on the
    intermediate `Call` — this is the assertion that would catch httpx
    helpfully un-encoding it again."""
    handler = json_ok({})
    step = Step(
        "fetch",
        "service",
        produces="r",
        kwargs=("identifier",),
        backend="client-service",
        call=Call("GET", "/v1/record/$identifier"),
    )
    async with transport(handler) as client:
        await run_service_step(
            step, backend, ctx_for(tmp_path, deps), {"identifier": "../../admin"}, client=client
        )

    assert str(handler.seen[0].url) == "https://api.example.com/v1/record/..%2F..%2Fadmin"


# ── B2: failure semantics ────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_a_4xx_fails_the_step_with_method_path_and_status(
    tmp_path: Path, deps: Deps, backend: Backend, status: int
) -> None:
    """Never a silent `None`. A flow that read a 403 as "no results" would
    publish nothing, report nothing, and look like it worked."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "Not authenticated"})

    step = Step(
        "select",
        "service",
        produces="p",
        backend="client-service",
        call=Call("GET", "/v1/workflows"),
    )
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed) as caught:
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert "GET" in str(caught.value)
    assert "/v1/workflows" in str(caught.value)
    assert str(status) in str(caught.value)
    assert "Not authenticated" in str(caught.value)


async def test_a_404_without_opt_in_fails(tmp_path: Path, deps: Deps, backend: Backend) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Record response not found"})

    step = Step(
        "fetch", "service", produces="r", backend="client-service", call=Call("GET", "/v1/x")
    )
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed):
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)


async def test_optional_404_skips_rather_than_failing(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """ "No response has been drafted yet" is a real answer to
    `GET /v1/submission/{id}` — and now expressible without pretending the
    call succeeded. Exactly what `ctx.skip` was built for."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    step = Step(
        "existing",
        "service",
        produces="existing",
        backend="client-service",
        call=Call("GET", "/v1/submission/RQ1", optional_404=True),
    )
    ctx = ctx_for(tmp_path, deps)
    async with transport(handler) as client:
        produced = await run_service_step(step, backend, ctx, {}, client=client)

    assert produced is None
    assert "404" in ctx.skipped


async def test_a_non_json_body_fails_rather_than_parsing_as_none(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """An HTML error page parsed as `None` is the silent-failure case wearing a
    different hat — a gateway timeout page is the common cause."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>504 Gateway Timeout</title></html>")

    step = Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x"))
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed, match="not JSON"):
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)


async def test_a_204_is_none_without_failing(tmp_path: Path, deps: Deps, backend: Backend) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    step = Step("x", "service", produces="p", backend="client-service", call=Call("DELETE", "/x"))
    async with transport(handler) as client:
        assert (
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)
            is None
        )


async def test_a_timeout_fails_rather_than_hanging(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    step = Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x"))
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed, match="failed"):
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)


# ── B3: credentials ──────────────────────────────────────────────────────────


async def test_the_first_set_credential_is_sent(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """This test used to assert the raw token, which is to say it encoded the
    bug production later found. The scheme is part of the credential."""
    handler = json_ok({})
    step = Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x"))
    async with transport(handler) as client:
        await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert handler.seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_the_token_appears_in_no_event_row(
    tmp_path: Path, deps: Deps, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted by scanning every serialised row for the literal secret, not by
    checking the three fields we remembered to check. A test that checks the
    fields we thought of proves only that we thought of three fields."""
    handler = json_ok({"ok": True})
    events = InMemoryEventLog()
    project = Project(root=tmp_path, backends={"client-service": backend})
    template = Template(
        name="t",
        steps=(
            Step(
                "x",
                "service",
                produces="p",
                backend="client-service",
                call=Call("GET", "/v1/workflows"),
            ),
        ),
    )

    async with transport(handler) as client:
        monkeypatch.setattr(
            "navigator_orchestrator.sdk.service.httpx.AsyncClient",
            lambda **kwargs: client,
        )
        monkeypatch.setattr(client, "aclose", _noop)
        await run_template(template, {}, ctx_for(tmp_path, deps, project), events=events)

    serialised = json.dumps(events.entries, default=str)
    assert TOKEN not in serialised, "the token reached the event log"
    assert "SERVICE_TOKEN" in serialised, "but the variable name must be there"


async def _noop() -> None:
    return None


def test_a_missing_credential_is_a_preflight_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("FALLBACK_TOKEN", raising=False)
    backend = Backend(
        name="client-service",
        base_url="https://x",
        token_env=("SERVICE_TOKEN", "FALLBACK_TOKEN"),
    )
    project = Project(root=tmp_path, backends={"client-service": backend})
    template = Template(
        name="t",
        steps=(
            Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x")),
        ),
    )

    missing = backend_requirements(template, project)
    assert [r.name for r in missing] == ["SERVICE_TOKEN"]
    assert "FALLBACK_TOKEN" in missing[0].why, "both acceptable variables are named"


async def test_a_run_without_a_credential_stops_before_the_first_call(
    tmp_path: Path, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing credential must cost nothing. Given that `secrets.sh` currently
    exports nothing at all, this is the most likely first-run failure there is."""
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("FALLBACK_TOKEN", raising=False)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        called.append(str(request.url))
        return httpx.Response(200, json={})

    backend = Backend(name="b", base_url="https://x", token_env=("SERVICE_TOKEN",))
    project = Project(root=tmp_path, backends={"b": backend})
    template = Template(
        name="t",
        steps=(Step("x", "service", produces="p", backend="b", call=Call("GET", "/x")),),
    )

    with pytest.raises(Blocked, match="SERVICE_TOKEN"):
        await run_template(template, {}, ctx_for(tmp_path, deps, project))
    assert called == [], "no request was made"


def test_a_backend_with_no_token_env_needs_no_credential(tmp_path: Path) -> None:
    """`GET /v1/records` is public. Demanding a credential would make an
    unauthenticated endpoint unreachable."""
    project = Project(root=tmp_path, backends={"b": Backend(name="b", base_url="https://x")})
    template = Template(
        name="t",
        steps=(Step("x", "service", produces="p", backend="b", call=Call("GET", "/x")),),
    )
    assert backend_requirements(template, project) == []


def test_an_unknown_backend_lists_the_configured_names(tmp_path: Path) -> None:
    project = Project(
        root=tmp_path, backends={"client-service": Backend("client-service", "https://x")}
    )
    step = Step("x", "service", produces="p", backend="client-servcie", call=Call("GET", "/x"))

    with pytest.raises(Exception, match="client-service"):
        resolve_backend(step, project)


def test_running_outside_a_project_says_what_declares_backends() -> None:
    step = Step("x", "service", produces="p", backend="b", call=Call("GET", "/x"))
    with pytest.raises(CallSpecError, match=r"navigator\-orchestrator\.toml"):
        resolve_backend(step, None)


# ── B4: retries ──────────────────────────────────────────────────────────────


async def test_a_5xx_then_a_200_succeeds(tmp_path: Path, deps: Deps, backend: Backend) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    step = Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x"))
    ctx = ctx_for(tmp_path, deps)
    async with transport(handler) as client:
        produced = await run_service_step(step, backend, ctx, {}, client=client)

    assert produced == {"ok": True}
    assert len(attempts) == 2
    assert "2 attempts" in ctx.notes[-1]


async def test_a_post_that_500s_is_not_retried(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """The single most important test in this file. `POST /v1/submission`
    creates an immutable document, and a timeout does not tell you whether the
    server committed it — retrying converts an uncertain outcome into a probable
    duplicate record response."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"detail": "boom"})

    step = Step(
        "store",
        "service",
        produces="p",
        backend="client-service",
        call=Call("POST", "/v1/submission", body={"a": 1}),
    )
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed):
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert len(attempts) == 1, f"the POST was sent {len(attempts)} times"


async def test_a_post_that_times_out_is_not_retried(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    step = Step("store", "service", produces="p", backend="client-service", call=Call("POST", "/x"))
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed):
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert len(attempts) == 1


async def test_exhausted_retries_fail_with_the_last_status(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(502, json={"detail": "bad gateway"})

    step = Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x"))
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed, match="502"):
            await run_service_step(
                step, backend, ctx_for(tmp_path, deps), {}, client=client, retries=1
            )

    assert len(attempts) == 2


async def test_a_4xx_is_not_retried(tmp_path: Path, deps: Deps, backend: Backend) -> None:
    """A refusal is an answer. Asking again is how a rate limit becomes a ban."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(403, json={"detail": "no"})

    step = Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x"))
    async with transport(handler) as client:
        with pytest.raises(ServiceFailed):
            await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert len(attempts) == 1


# ── B5: what the run records ─────────────────────────────────────────────────


async def test_the_run_records_the_declared_path_not_the_interpolated_one(
    tmp_path: Path, deps: Deps, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interpolated path is where an identifier that is also personal data
    ends up. `detail` is a summary (SPEC-EDW-002 §5)."""
    handler = json_ok({"ok": True})
    events = InMemoryEventLog()
    project = Project(root=tmp_path, backends={"client-service": backend})
    template = Template(
        name="t",
        params=("identifier",),
        steps=(
            Step(
                "fetch",
                "service",
                produces="r",
                kwargs=("identifier",),
                backend="client-service",
                call=Call("GET", "/v1/record/$identifier"),
            ),
        ),
    )

    async with transport(handler) as client:
        monkeypatch.setattr(
            "navigator_orchestrator.sdk.service.httpx.AsyncClient",
            lambda **kwargs: client,
        )
        monkeypatch.setattr(client, "aclose", _noop)
        result = await run_template(
            template,
            {},
            ctx_for(tmp_path, deps, project, identifier="private-person-123"),
            events=events,
        )

    assert result.pool["r"] == {"ok": True}
    note = result.notes[-1]
    assert "/v1/record/$identifier" in note
    assert "private-person-123" not in note


async def test_a_failed_call_fails_the_run_and_is_logged(
    tmp_path: Path, deps: Deps, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Not authenticated"})

    events = InMemoryEventLog()
    project = Project(root=tmp_path, backends={"client-service": backend})
    template = Template(
        name="t",
        steps=(
            Step("x", "service", produces="p", backend="client-service", call=Call("GET", "/x")),
        ),
    )

    async with transport(handler) as client:
        monkeypatch.setattr(
            "navigator_orchestrator.sdk.service.httpx.AsyncClient",
            lambda **kwargs: client,
        )
        monkeypatch.setattr(client, "aclose", _noop)
        with pytest.raises(StepFailed):
            await run_template(template, {}, ctx_for(tmp_path, deps, project), events=events)

    row = next(e for e in events.entries if e["status"] == "failed" and e["step"] == "x")
    assert row["detail"]["error"] == "ServiceFailed"


async def test_a_service_step_needs_no_hook_and_is_sourced_to_the_engine(
    tmp_path: Path, deps: Deps, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = json_ok([1, 2, 3])
    project = Project(root=tmp_path, backends={"client-service": backend})
    template = Template(
        name="t",
        steps=(
            Step(
                "x",
                "service",
                produces="p",
                backend="client-service",
                call=Call("GET", "/v1/records"),
            ),
        ),
    )

    async with transport(handler) as client:
        monkeypatch.setattr(
            "navigator_orchestrator.sdk.service.httpx.AsyncClient",
            lambda **kwargs: client,
        )
        monkeypatch.setattr(client, "aclose", _noop)
        result = await run_template(template, {}, ctx_for(tmp_path, deps, project))

    assert result.steps[0].source == "engine"
    assert result.pool["p"] == [1, 2, 3]


# ── found by B6: public endpoints ────────────────────────────────────────────


def test_a_public_call_needs_no_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Found by running it, not by reading it. Requiring a token per *backend*
    blocked the catalogue template at preflight even though every call it makes
    is public — `GET /v1/records` and `GET /v1/workflows` sit on the same host
    with different rules, so the requirement follows the endpoint."""
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("FALLBACK_TOKEN", raising=False)
    backend = Backend(name="b", base_url="https://x", token_env=("SERVICE_TOKEN",))
    project = Project(root=tmp_path, backends={"b": backend})
    template = Template(
        name="t",
        steps=(
            Step("x", "service", produces="p", backend="b", call=Call("GET", "/x", public=True)),
        ),
    )
    assert backend_requirements(template, project) == []


def test_one_authenticated_call_makes_the_credential_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A template mixing public and private calls still needs the credential —
    the requirement is the union, not the last step read."""
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("FALLBACK_TOKEN", raising=False)
    backend = Backend(name="b", base_url="https://x", token_env=("SERVICE_TOKEN",))
    project = Project(root=tmp_path, backends={"b": backend})
    template = Template(
        name="t",
        steps=(
            Step("a", "service", produces="p", backend="b", call=Call("GET", "/pub", public=True)),
            Step("b", "service", produces="q", backend="b", call=Call("GET", "/private")),
        ),
    )
    assert [r.name for r in backend_requirements(template, project)] == ["SERVICE_TOKEN"]


async def test_a_public_call_sends_no_authorization_header(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """Sending one anyway would leak an SERVICE token to an endpoint that
    never needed it, and to any proxy in front of it."""
    handler = json_ok([])
    step = Step(
        "x",
        "service",
        produces="p",
        backend="client-service",
        call=Call("GET", "/v1/records", public=True),
    )
    async with transport(handler) as client:
        await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert "Authorization" not in handler.seen[0].headers


def test_a_call_is_authenticated_unless_it_says_otherwise() -> None:
    """Fail-closed is the direction you get by saying nothing. A call wrongly
    marked public gets a 403, which is loud; the wrong default the other way
    makes a public endpoint unreachable and looks like a missing password."""
    assert Call("GET", "/x").public is False


# ── found against production: the Bearer scheme ──────────────────────────────


async def test_the_token_is_sent_as_a_bearer_credential(
    tmp_path: Path, deps: Deps, backend: Backend
) -> None:
    """Found against production, not in review. The executor sent the raw token;
    `decorators/tiers.py` requires the header to start with `"Bearer "` and
    returns `None` otherwise, so a **valid** credential read as anonymous and
    came back 403 — indistinguishable from a wrong one.

    B6 missed it because the only call it made was public, which is a fair
    criticism of the proof rather than of the stage."""
    handler = json_ok([])
    step = Step(
        "x", "service", produces="p", backend="client-service", call=Call("GET", "/v1/workflows")
    )
    async with transport(handler) as client:
        await run_service_step(step, backend, ctx_for(tmp_path, deps), {}, client=client)

    assert handler.seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("stored", ["tok", "Bearer tok", "bearer tok", "  tok  "])
def test_an_already_prefixed_token_is_not_prefixed_twice(stored: str) -> None:
    """Whether the scheme is stored in the environment variable is the
    operator's business, and both conventions are in the wild. `Bearer Bearer x`
    is a 403 nobody would think to look for."""
    assert bearer(stored).lower().count("bearer") == 1
    assert bearer(stored).endswith("tok")
