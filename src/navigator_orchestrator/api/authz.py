"""Principal resolution (SPEC-AIP-003 §3.5).

Deliberately **not** full authorization: no user store, no roles, no
per-workflow permissions. This is the minimum that makes an audit record mean
something — an entry whose actor is always `anonymous` demonstrates no
separation of duties, which is the property R1 exists to provide.

Real authorization is a later spec. The seam is here so it isn't retrofitted.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Request

from navigator_orchestrator.store import Principal

__all__ = [
    "ANONYMOUS",
    "HeaderPrincipalProvider",
    "Principal",
    "PrincipalProvider",
    "RequiredPrincipal",
    "StubPrincipalProvider",
    "current_principal",
    "require_principal",
    "resolve_principal",
]

#: R1 dev default. Fine for reading; rejected for deciding when
#: `NAVIGATOR_REQUIRE_PRINCIPAL` is on.
ANONYMOUS = Principal(subject="anonymous", scopes=frozenset({"workflows:run"}), issuer="stub")


class PrincipalProvider(Protocol):
    def __call__(self, request: Request) -> Principal | None: ...


class StubPrincipalProvider:
    """Everyone is `anonymous`. Development only."""

    def __call__(self, request: Request) -> Principal | None:
        return ANONYMOUS


class HeaderPrincipalProvider:
    """Trust an identity header set by an upstream proxy.

    For UAT this is Cloud Run behind GCP IAP, which sets
    `x-goog-iap-jwt-assertion` and — critically — strips any client-supplied
    copy. **This provider is only safe when such a proxy is in front of it**:
    on its own the header is caller-controlled and therefore worthless. R1
    verifies presence, not signature; signature verification is part of real
    authz, and pretending otherwise here would be worse than being explicit.
    """

    def __init__(self, header: str, issuer: str = "header") -> None:
        self._header = header
        self._issuer = issuer

    def __call__(self, request: Request) -> Principal | None:
        raw = request.headers.get(self._header)
        if not raw:
            return None
        return Principal(subject=raw.strip(), issuer=self._issuer)


def resolve_principal(request: Request) -> Principal | None:
    """Run the configured provider. `None` means 'no identity available'."""
    context = getattr(request.app.state, "context", None)
    provider: PrincipalProvider = (
        context.principal_provider if context is not None else StubPrincipalProvider()
    )
    return provider(request)


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency for acts that must be attributable.

    Rejects an unattributable decision with a 403 that names the missing
    credential, rather than silently writing `anonymous` into the audit log
    (AC-5).
    """
    context = getattr(request.app.state, "context", None)
    settings = context.settings if context is not None else None
    principal = resolve_principal(request)

    unattributable = principal is None or principal.is_anonymous
    if settings is not None and settings.require_principal and unattributable:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "principal_required",
                "detail": (
                    f"a decision must be attributable; supply the "
                    f"{settings.principal_header!r} header "
                    f"(or set NAVIGATOR_REQUIRE_PRINCIPAL=false for local dev)"
                ),
            },
        )
    return principal or ANONYMOUS


async def current_principal(request: Request) -> Principal:
    """Best-effort identity. Never rejects.

    Starting a run is not an audit act, so it does not need an attributable
    actor — only a *decision* does.
    """
    return resolve_principal(request) or ANONYMOUS


#: For ordinary requests: identity if available, `anonymous` otherwise.
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
#: For acts that go into the audit log — rejects when unattributable (AC-5).
RequiredPrincipal = Annotated[Principal, Depends(require_principal)]
