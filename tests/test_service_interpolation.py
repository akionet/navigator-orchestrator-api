"""B1 — the call is data, and the pool never chooses the structure (SPEC-NSP-006 §2.1).

Written before the executor, and network-free on purpose. This module is where
an injection would live: a pool value can hold model output, and model output
concatenated into a URL is how a path traversal gets in. Building and reviewing
it with no transport in sight is what makes that claim checkable.
"""

from __future__ import annotations

import pytest

from navigator_orchestrator.sdk.service import (
    Call,
    CallSpecError,
    collect_placeholders,
    interpolate,
    should_retry,
    validate_call,
)
from navigator_orchestrator.sdk.templates import Step

# Values a record title, an AI draft or a hostile caller could plausibly hold.
NASTY = [
    "../../admin",
    "a/b/c",
    "?admin=1",
    "x&y=2",
    "with space",
    "100%",
    "#fragment",
    "semi;colon",
    "unicode-café",
    "'; DROP TABLE records--",
    "$other",
    "x" * 4000,
]


# ── whole-value substitution ─────────────────────────────────────────────────


def test_a_query_value_is_substituted() -> None:
    call = Call("GET", "/v1/workflows", query={"status": "$status"})
    assert interpolate(call, {"status": "PENDING"}, ("status",)).query == {"status": "PENDING"}


def test_a_whole_path_segment_is_substituted() -> None:
    call = Call("PATCH", "/v1/workflows/$workflow_id/status")
    resolved = interpolate(call, {"workflow_id": "RH8FMGQ-WF"}, ("workflow_id",))
    assert resolved.path == "/v1/workflows/RH8FMGQ-WF/status"


def test_a_nested_body_value_is_substituted() -> None:
    call = Call("POST", "/v1/submission", body={"requestId": "$request_id", "tags": ["$diet"]})
    resolved = interpolate(call, {"request_id": "RQ1", "diet": "sanctions"}, ("request_id", "diet"))
    assert resolved.body == {"requestId": "RQ1", "tags": ["sanctions"]}


def test_a_non_string_pool_value_keeps_its_type() -> None:
    """`{"servings": 4}` must not become `{"servings": "4"}` — a body is JSON,
    and a service that validates types would reject the string."""
    call = Call("POST", "/v1/record", body={"servings": "$servings", "live": "$live"})
    resolved = interpolate(call, {"servings": 4, "live": True}, ("servings", "live"))
    assert resolved.body == {"servings": 4, "live": True}


def test_literal_text_is_left_alone() -> None:
    call = Call("GET", "/v1/records", query={"type": "meal"})
    assert interpolate(call, {}, ()).query == {"type": "meal"}


# ── encoding: the point of the rule ──────────────────────────────────────────


@pytest.mark.parametrize("value", NASTY)
def test_a_path_value_stays_exactly_one_segment(value: str) -> None:
    """The property test. Whatever a pool value contains, it cannot add,
    remove or escape a path segment — which is the whole claim of §2.1."""
    call = Call("GET", "/v1/record/$identifier")
    resolved = interpolate(call, {"identifier": value}, ("identifier",))

    before, _, after = resolved.path.partition("/v1/record/")
    assert before == "" and after, "the declared prefix survives intact"
    assert "/" not in after, f"{value!r} escaped its segment as {after!r}"
    assert "?" not in after and "#" not in after, "no query or fragment injected"
    assert resolved.path.count("/") == call.path.count("/"), "segment count unchanged"


def test_a_slash_in_a_value_is_encoded_not_split() -> None:
    resolved = interpolate(Call("GET", "/v1/record/$id"), {"id": "a/b"}, ("id",))
    assert resolved.path == "/v1/record/a%2Fb"


def test_a_query_value_is_left_for_the_client_to_encode() -> None:
    """Encoding here as well would double-encode it — `a b` would reach the
    service as `a%2520b`."""
    resolved = interpolate(Call("GET", "/x", query={"q": "$q"}), {"q": "a b"}, ("q",))
    assert resolved.query == {"q": "a b"}


# ── the refused fragment case ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        Call("GET", "/x", query={"q": "status:$status AND live"}),
        Call("GET", "/x", query={"q": "$status-suffix"}),
        Call("GET", "/v1/record-$identifier"),
        Call("POST", "/x", body={"note": "for $author"}),
        Call("POST", "/x", body={"tags": ["prefix$tag"]}),
    ],
    ids=["query-mid", "query-suffix", "path-fragment", "body-string", "body-list"],
)
def test_a_placeholder_inside_a_longer_string_is_refused(call: Call) -> None:
    """A whole value can be encoded; a fragment cannot, because by the time it
    is substituted the structure is already decided."""
    with pytest.raises(CallSpecError, match="whole value"):
        validate_call(call, ("status", "identifier", "author", "tag"))


def test_the_refusal_names_where_it_is() -> None:
    with pytest.raises(CallSpecError, match="query contains"):
        validate_call(Call("GET", "/x", query={"q": "a$b"}), ("b",))


# ── declared inputs only ─────────────────────────────────────────────────────


def test_an_undeclared_placeholder_is_refused_with_what_is_declared() -> None:
    """The same superset-binding rule every other executor follows, so what a
    call can see stays visible in the template."""
    with pytest.raises(CallSpecError) as caught:
        validate_call(Call("GET", "/v1/record/$secret"), ("identifier",))

    assert "$secret" in str(caught.value)
    assert "identifier" in str(caught.value)


def test_a_missing_pool_value_is_refused_not_stringified() -> None:
    """Substituting `None` is how a request goes to `/v1/record/None` and 404s
    mysteriously an hour later."""
    with pytest.raises(CallSpecError, match="not in the pool"):
        interpolate(Call("GET", "/v1/record/$identifier"), {}, ("identifier",))


def test_collect_finds_placeholders_everywhere() -> None:
    call = Call("POST", "/v1/$a/x", query={"q": "$b"}, body={"deep": [{"k": "$c"}]})
    assert collect_placeholders(call) == {"a", "b", "c"}


# ── the Call and Step declarations ───────────────────────────────────────────


def test_a_path_must_be_absolute() -> None:
    with pytest.raises(CallSpecError, match="must start with"):
        Call("GET", "v1/records")


def test_the_method_is_normalised() -> None:
    assert Call("get", "/x").method == "GET"


def test_a_service_step_without_a_call_is_refused_at_declaration() -> None:
    with pytest.raises(ValueError, match="needs call="):
        Step("select", "service", produces="pending")


def test_a_non_service_step_cannot_carry_a_call() -> None:
    with pytest.raises(ValueError, match="but is a"):
        Step("draft", "agent", produces="d", call=Call("GET", "/x"))


def test_a_service_step_needs_no_hook() -> None:
    """The engine implements it, so `check` must not demand an implementation."""
    assert Step("select", "service", produces="p", call=Call("GET", "/x")).required is False


# ── retry policy ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [500, 502, 503, 429, None])
def test_an_idempotent_method_retries_transient_failures(status: int | None) -> None:
    assert should_retry("GET", status=status, attempt=1, limit=3) is True


@pytest.mark.parametrize("status", [500, 503, 429, None])
def test_post_is_never_retried(status: int | None) -> None:
    """`POST /v1/submission` creates an immutable document and a timeout
    does not say whether the server committed it. Retrying converts an
    uncertain outcome into a probable duplicate."""
    assert should_retry("POST", status=status, attempt=1, limit=3) is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_4xx_is_never_retried(status: int) -> None:
    """A refusal is an answer. Asking again is how a rate limit becomes a ban."""
    assert should_retry("GET", status=status, attempt=1, limit=3) is False


def test_retries_are_bounded() -> None:
    assert should_retry("GET", status=500, attempt=3, limit=3) is False


def test_patch_retries_because_the_status_route_sets_an_absolute_value() -> None:
    assert should_retry("PATCH", status=500, attempt=1, limit=3) is True
