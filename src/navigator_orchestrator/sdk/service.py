"""The `service` executor — calling a backend API from a step (SPEC-NSP-006).

> *"A workflow CLI that can be configured to work with any backend api and/or
> shell script"*

`shell` covered the second half. This is the first, and it is what every
editorial flow leans on: F1 cannot select a pending request without it, and F2
cannot publish.

## The call is data

```python
Step(
    "select", "service", produces="pending", kwargs=("status",),
    backend="client-service",
    call=Call("GET", "/v1/workflows", query={"status": "$status"}),
)
```

`Call` holds no callable, for the same reason `uses` exists
(`SPEC-NSP-005` §5): the moment a call is a lambda, a YAML template stops being
a parser and becomes a redesign.

## Interpolation is structural, never textual

This is the one rule in this module worth reading twice.

A `$name` may be **an entire path segment, an entire query value, or an entire
JSON body value.** It may never be a fragment glued into a longer string.

```python
query={"status": "$status"}                  # allowed
path="/v1/workflows/$workflow_id/status"     # allowed - a whole segment
query={"q": "status:$status AND live"}       # REFUSED, at check time
```

A pool value can hold model output. Model output concatenated into a URL is how
a path traversal or an injected query parameter gets in. A whole value can be
**percent-encoded** — one segment stays one segment however many slashes it
contains — whereas a fragment inside a larger string cannot be, because by the
time it is substituted the structure is already decided. Refusing the fragment
case costs one rule and removes the class.

## A non-2xx is a failed step, never a silent `None`

An editorial flow that treated a 403 as "no results" would publish nothing,
report nothing, and look like it worked. That is strictly worse than crashing,
because nobody investigates a success. `optional_404` exists for the one honest
case — "no response has been drafted yet" — and is opt-in per step, because
defaulting it would reintroduce the silent `None` through the back door.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from navigator_orchestrator.sdk.context import Ctx
from navigator_orchestrator.sdk.preflight import Requirement
from navigator_orchestrator.sdk.project import Backend
from navigator_orchestrator.sdk.templates import Step

__all__ = [
    "Backend",
    "Call",
    "CallSpecError",
    "ServiceFailed",
    "backend_requirements",
    "bearer",
    "collect_placeholders",
    "interpolate",
    "resolve_backend",
    "run_service_step",
    "should_retry",
    "validate_call",
]

#: A whole-value placeholder, optionally selecting into a product with dots:
#: `$request._requestId`. Anchored, so `"status:$status"` does not match and
#: is refused rather than half-substituted — see the module docstring.
#:
#: Dots select; they do not weaken anything. What is substituted is still a
#: **whole** path segment, query value or body value, so it is still
#: encodable. The alternative was a flattening step per identifier, which
#: would put three steps in a template to move three strings.
PLACEHOLDER = re.compile(r"^\$([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)$")

#: Any `$name` occurrence, used only to *detect* the refused fragment case.
ANY_PLACEHOLDER = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)")

#: Methods safe to retry. `PATCH` is here because the one PATCH the editorial
#: flows use — `/v1/workflows/{id}/status` — sets an absolute value rather than
#: incrementing one. `POST` is deliberately absent; see `should_retry`.
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "PATCH"})

#: How much of an error body reaches the failure message.
BODY_LIMIT = 500

#: Status codes that mean "try again", as opposed to "no".
TOO_MANY_REQUESTS = 429
SERVER_ERROR = 500
CLIENT_ERROR = 400
NOT_FOUND = 404
NO_CONTENT = 204

#: Longest we will honour a `Retry-After` for.
MAX_RETRY_WAIT = 30.0


class CallSpecError(ValueError):
    """The call is malformed. Raised at `check`, never mid-run."""


class ServiceFailed(RuntimeError):
    """The call was refused, failed, or timed out."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Call:
    """One HTTP request, declared as data."""

    method: str
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    #: Treat a 404 as "this does not exist yet" — recorded as `skipped`, `None`
    #: into the pool. Opt-in per call; see the module docstring.
    optional_404: bool = False
    #: This endpoint needs no credential. Declared per **call**, not per
    #: backend, because that is where the truth lives: `GET /v1/records` is
    #: public and `GET /v1/workflows` is SERVICE, on the same host. Defaults
    #: to `False` so the fail-closed direction is the one you get by saying
    #: nothing — a call wrongly marked public gets a 403, which is loud, while
    #: a wrong default the other way makes a public endpoint unreachable and
    #: looks like a missing password.
    public: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        if not self.path.startswith("/"):
            raise CallSpecError(f"call path must start with '/', got {self.path!r}")


def collect_placeholders(call: Call) -> set[str]:
    """Every `$name` the call refers to, wherever it appears."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(ANY_PLACEHOLDER.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(call.path)
    walk(call.query)
    walk(call.body)
    return found


def validate_call(call: Call, allowed: tuple[str, ...]) -> None:
    """Reject a malformed call **before the run starts** (`SPEC-NSP-006` §7).

    Two classes, both template mistakes and both free to detect:

    - a `$name` the step did not declare in `kwargs` — the same
      superset-binding rule every other executor follows, so what a call can see
      stays visible in the template;
    - a `$name` embedded in a longer string — the fragment case.
    """
    for name in sorted(collect_placeholders(call)):
        # The *root* must be declared. The dotted tail selects inside the
        # product that root names, so declaring `candidate` grants the whole
        # candidate either way — a tail is a convenience, not a new grant.
        root = name.split(".")[0]
        if root not in allowed:
            declared = ", ".join(allowed) or "none"
            raise CallSpecError(
                f"call refers to ${name}, which the step does not declare; "
                f"add {root!r} to kwargs (declared: {declared})"
            )

    for where, value in (("path", call.path), ("query", call.query), ("body", call.body)):
        fragment = _first_fragment(value)
        if fragment is not None:
            raise CallSpecError(
                f"{where} contains {fragment!r}: a $name must be a whole value, "
                f"not part of a larger string. A whole value can be encoded; a "
                f"fragment cannot, because the structure is already decided "
                f"(SPEC-NSP-006 §2.1)"
            )


def _first_fragment(value: Any) -> str | None:
    """The first string that mixes a `$name` with anything else.

    The path is exempt from the *slash* separator only: `"/v1/x/$id/status"` is
    a sequence of whole segments, which is why it is split before checking.
    """
    if isinstance(value, str):
        for part in value.split("/"):
            if not part or PLACEHOLDER.match(part):
                continue
            if ANY_PLACEHOLDER.search(part):
                return value
        return None
    if isinstance(value, dict):
        for item in value.values():
            found = _first_fragment(item)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _first_fragment(item)
            if found is not None:
                return found
    return None


def interpolate(call: Call, pool: dict[str, Any], allowed: tuple[str, ...]) -> Call:
    """Substitute pool values into the call. Whole values only.

    Path segments are percent-encoded, so a value containing `/` produces one
    segment rather than two. Query values are left as data for httpx to encode —
    encoding them here would double-encode them.
    """
    validate_call(call, allowed)

    def value_of(name: str) -> Any:
        root, *tail = name.split(".")
        if root not in pool:
            # Not `None`: substituting a missing value as the string "None" is
            # how a request goes to /v1/record/None and 404s mysteriously.
            raise CallSpecError(f"${name} is not in the pool; no step has produced it yet")
        value: Any = pool[root]
        for part in tail:
            if not isinstance(value, dict) or part not in value:
                available = ", ".join(sorted(value)) if isinstance(value, dict) else "not a mapping"
                raise CallSpecError(f"${name}: {root!r} has no {part!r} ({available})")
            value = value[part]
        return value

    segments = []
    for part in call.path.split("/"):
        match = PLACEHOLDER.match(part)
        if match is None:
            segments.append(part)
            continue
        # `safe=""` so a slash inside the value is encoded rather than becoming
        # a path separator. This single argument is most of what §2.1 buys.
        segments.append(quote(str(value_of(match.group(1))), safe=""))

    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            match = PLACEHOLDER.match(value)
            return value_of(match.group(1)) if match else value
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [substitute(item) for item in value]
        return value

    return Call(
        method=call.method,
        path="/".join(segments),
        query={key: substitute(item) for key, item in call.query.items()},
        body=substitute(call.body),
        optional_404=call.optional_404,
    )


def bearer(token: str) -> str:
    """The `Authorization` header value for a token.

    **Found against production, not in review.** The executor sent the raw token
    and client-service refused it: `decorators/tiers.py` requires the header to
    start with `"Bearer "` and returns `None` otherwise, so a valid credential
    read as anonymous and came back 403 — indistinguishable from a wrong token.

    B6 missed it because the one call it made was public. That is a fair
    criticism of the proof, not of the stage: the fix is to make the *first*
    authenticated call part of it too.

    An already-prefixed value is passed through, because whether the scheme is
    stored in the environment variable is the operator's business and both
    conventions are in the wild.
    """
    stripped = token.strip()
    return stripped if stripped.lower().startswith("bearer ") else f"Bearer {stripped}"


def should_retry(method: str, *, status: int | None, attempt: int, limit: int) -> bool:
    """Whether to try again.

    **`POST` is never retried.** `POST /v1/submission` creates a document
    the route documents as immutable, and a timeout does not tell you whether
    the server committed it — retrying converts an uncertain outcome into a
    probable duplicate. A failed `POST` is a human's problem, and the event log
    has to be good enough to hand it to them.
    """
    if attempt >= limit or method not in IDEMPOTENT:
        return False
    if status is None:  # a connection error or a timeout
        return True
    return status == TOO_MANY_REQUESTS or status >= SERVER_ERROR


def retry_after(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait. Honours `Retry-After` when the server sends one —
    guessing against a server that has told you the answer is impolite and
    usually wrong."""
    if response is not None:
        raw: str = str(response.headers.get("retry-after", ""))
        try:
            # Capped: a server asking us to wait an hour is asking us to
            # hold a checkpoint open for an hour, and the answer is no.
            wait: float = min(float(raw), MAX_RETRY_WAIT)
            return max(0.0, wait)
        except ValueError:
            pass
    return 0.5 * (2.0**attempt)


def resolve_backend(step: Step, project: Any) -> Backend:
    """The `Backend` a step names, from the loaded manifest.

    Both failures name the fix rather than the symptom: running outside a
    project, and naming a backend the project does not declare. The second is a
    typo the first time and a stale template the second, and both are cheaper to
    read than a connection error to `None`.
    """
    if not step.backend:
        raise CallSpecError(f"service step {step.name!r} does not name a backend=")
    if project is None:
        raise CallSpecError(
            f"step {step.name!r} calls backend {step.backend!r}, but this run is "
            f"not inside a workflow project; a navigator-orchestrator.toml is what declares "
            f"backends (SPEC-NSP-005 §4)"
        )
    backend = project.backend(step.backend)
    if not isinstance(backend, Backend):  # pragma: no cover - defensive
        raise CallSpecError(f"backend {step.backend!r} did not resolve to a Backend")
    return backend


def _needs_credential(template: Any, backend_name: str) -> bool:
    """Whether any call to this backend actually needs a credential.

    Found by running it (`PLAN-NSP-R2-006` B6): requiring a token per *backend*
    blocked the catalogue template at preflight, even though every call it makes
    is public. The requirement follows the endpoints, not the host.
    """
    return any(
        step.executor == "service"
        and step.backend == backend_name
        and not getattr(step.call, "public", False)
        for step in getattr(template, "steps", ())
    )


def backend_requirements(template: Any, project: Any) -> list[Requirement]:
    """Credential requirements contributed by a template's `service` steps.

    Folded into preflight so a run with no credential stops **before the first
    step** rather than after the expensive one (`SPEC-NSP-006` §5). Given that
    `~/code/secrets.sh` currently aborts on a malformed line and exports
    nothing, this is the single most likely first-run failure — so it is the one
    that most needs to explain itself.

    A backend with no `token_env` contributes nothing: not every API needs a
    credential, and demanding one would make an unauthenticated endpoint
    unreachable.
    """
    seen: set[str] = set()
    out: list[Requirement] = []
    for step in getattr(template, "steps", ()):
        if step.executor != "service" or not step.backend or step.backend in seen:
            continue
        seen.add(step.backend)
        try:
            backend = resolve_backend(step, project)
        except CallSpecError:
            # `check` reports an unknown backend properly. Preflight's job is
            # credentials, and failing here would report the wrong problem.
            continue
        if (
            _needs_credential(template, step.backend)
            and backend.token_env
            and backend.token() is None
        ):
            out.append(
                Requirement(
                    name=backend.token_env[0],
                    why=(
                        f"backend {backend.name!r} needs a bearer token; set one of "
                        f"{', '.join(backend.token_env)}"
                    ),
                )
            )
    return out


async def run_service_step(
    step: Step,
    backend: Backend,
    ctx: Ctx,
    pool: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    retries: int = 2,
) -> Any:
    """Perform the step's call and return the parsed body.

    `client` is injectable so tests drive an `httpx.MockTransport` and assert on
    the `Request` the client would really send, rather than on a mock of our own
    design agreeing with us.
    """
    if step.call is None:  # pragma: no cover - `check` rejects this first
        raise CallSpecError(f"service step {step.name!r} has no call=")

    resolved = interpolate(step.call, pool, step.kwargs)
    # A public call sends no Authorization header at all. Sending one anyway
    # would leak an SERVICE token to an endpoint that never needed it, and to
    # any proxy in front of it.
    credential = None if step.call.public else backend.token()
    headers = {"Accept": "application/json"}
    if credential is not None:
        headers["Authorization"] = bearer(credential[0])

    owned = client is None
    http = client or httpx.AsyncClient(timeout=backend.timeout)
    started = time.monotonic()
    try:
        response, attempts = await _send(http, backend, resolved, headers, retries)
    finally:
        if owned:
            await http.aclose()
    elapsed_ms = round((time.monotonic() - started) * 1000)

    # What the event row carries (SPEC-NSP-006 §6). The path is the **declared**
    # one, uninterpolated: an interpolated path is where an identifier that is
    # also personal data ends up. And it is the credential *variable name*,
    # never the token -- without which "no run has used FALLBACK_TOKEN in a
    # month" is unanswerable, and the legacy token can never be retired.
    ctx.detail.update(
        backend=backend.name,
        method=resolved.method,
        path=step.call.path,
        http_status=response.status_code,
        elapsed_ms=elapsed_ms,
        attempts=attempts,
        auth=credential[1] if credential else "none",
    )
    ctx.note(
        f"{resolved.method} {step.call.path} -> {response.status_code} "
        f"({attempts} attempt{'s' if attempts != 1 else ''}, "
        f"auth: {credential[1] if credential else 'none'})"
    )

    if response.status_code == NOT_FOUND and step.call.optional_404:
        ctx.skip(f"404 from {resolved.method} {step.call.path}")
        return None
    if response.status_code >= CLIENT_ERROR:
        raise ServiceFailed(
            f"{resolved.method} {backend.name}{step.call.path} -> "
            f"{response.status_code}: {response.text.strip()[:BODY_LIMIT] or 'no body'}",
            status=response.status_code,
        )
    return _parse(response, resolved, backend)


async def _send(
    http: httpx.AsyncClient,
    backend: Backend,
    call: Call,
    headers: dict[str, str],
    retries: int,
) -> tuple[httpx.Response, int]:
    """Send, retrying only what `should_retry` permits."""
    url = f"{backend.base_url}{call.path}"
    attempt = 0
    while True:
        attempt += 1
        response: httpx.Response | None = None
        try:
            response = await http.request(
                call.method,
                url,
                params=call.query or None,
                json=call.body if call.body is not None else None,
                headers=headers,
                timeout=backend.timeout,
            )
            if not should_retry(
                call.method, status=response.status_code, attempt=attempt, limit=retries + 1
            ):
                return response, attempt
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if not should_retry(call.method, status=None, attempt=attempt, limit=retries + 1):
                raise ServiceFailed(
                    f"{call.method} {backend.name}{call.path} failed: {exc}"
                ) from exc
        await asyncio.sleep(retry_after(response, attempt))


def _parse(response: httpx.Response, call: Call, backend: Backend) -> Any:
    """Parse a successful response.

    An HTML error page parsed as `None` is the silent-failure case wearing a
    different hat, so an unparseable body fails the step and shows what came
    back instead.
    """
    if response.status_code == NO_CONTENT or not response.content:
        return None
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ServiceFailed(
            f"{call.method} {backend.name}{call.path} returned {response.status_code} "
            f"but the body is not JSON: {response.text.strip()[:200]!r}",
            status=response.status_code,
        ) from exc
