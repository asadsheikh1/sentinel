"""Request authentication and per-route access declarations.

Every route must declare exactly one access rule by depending on one of:

* ``public()``         — no authentication (health, login)
* ``authenticated()``  — any logged-in user
* ``require(perm)``    — a logged-in user whose role grants ``perm``
* ``require_scope(s)`` — a machine API key carrying scope ``s``
* ``internal_only()``  — SENTINEL's own reverse proxy (shared secret)
* ``ingest_token()``   — a SENTINEL Agent, with the ingest token of the integration
                         named in the path (``{integration_id}``)

The dependency callables carry an ``ACCESS_ATTR`` marker. A test walks every
route in the app and fails if one has no marker, so a new endpoint can't ship
unprotected by accident. ``docs/rbac.md`` is generated from the same markers.
"""

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status

from sentinel.api.deps import DbSession
from sentinel.auth.permissions import KeyScope, Permission
from sentinel.auth.service import Actor, AuthService, KeyPrincipal, SessionPrincipal
from sentinel.auth.tokens import constant_time_equals
from sentinel.db.models import ActorType
from sentinel.integrations.gate import IngestGate, TokenInfo
from sentinel.integrations.tokens import bearer_token

SESSION_COOKIE = "sentinel_session"
CSRF_COOKIE = "sentinel_csrf"
CSRF_HEADER = "X-CSRF-Token"
API_KEY_HEADER = "X-Sentinel-Key"
INTERNAL_AUTH_HEADER = "X-Sentinel-Internal-Auth"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ACCESS_ATTR = "__sentinel_access__"


@dataclass(frozen=True)
class Access:
    kind: Literal["public", "authenticated", "permission", "scope", "internal", "ingest"]
    value: str | None = None

    def describe(self) -> str:
        return {
            "public": "public",
            "authenticated": "any signed-in user",
            "permission": f"`{self.value}`",
            "scope": f"API key scope `{self.value}`",
            "internal": "internal proxy secret",
            "ingest": "the integration's ingest token",
        }[self.kind]


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required.")


def _forbidden(detail: str = "You don't have permission to do this.") -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail)


def client_ip(request: Request) -> str | None:
    """The peer IP, or None if it isn't a valid address (unix socket, test client)."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def auth_service(request: Request, db: DbSession) -> AuthService:
    return AuthService(db, request.app.state.settings)


AuthSvc = Annotated[AuthService, Depends(auth_service)]


async def _session_principal(request: Request, svc: AuthService) -> SessionPrincipal:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _unauthorized()
    principal = await svc.authenticate_session(token)
    if principal is None:
        raise _unauthorized()
    if request.method not in SAFE_METHODS and not svc.csrf_valid(
        principal.session, request.headers.get(CSRF_HEADER)
    ):
        raise _forbidden("CSRF token missing or invalid.")
    request.state.principal = principal
    return principal


def _mark[F](fn: F, access: Access) -> F:
    setattr(fn, ACCESS_ATTR, access)
    return fn


def public() -> Callable[[], None]:
    def dep() -> None:
        return None

    return _mark(dep, Access("public"))


def authenticated() -> Callable[[Request, AuthService], Awaitable[SessionPrincipal]]:
    async def dep(request: Request, svc: AuthSvc) -> SessionPrincipal:
        return await _session_principal(request, svc)

    return _mark(dep, Access("authenticated"))


def require(
    permission: Permission,
) -> Callable[[Request, AuthService], Awaitable[SessionPrincipal]]:
    async def dep(request: Request, svc: AuthSvc) -> SessionPrincipal:
        principal = await _session_principal(request, svc)
        if permission not in principal.permissions:
            raise _forbidden()
        return principal

    return _mark(dep, Access("permission", permission.value))


def require_scope(scope: KeyScope) -> Callable[[Request, AuthService], Awaitable[KeyPrincipal]]:
    async def dep(request: Request, svc: AuthSvc) -> KeyPrincipal:
        presented = request.headers.get(API_KEY_HEADER)
        if not presented:
            raise _unauthorized()
        principal = await svc.authenticate_key(presented)
        if principal is None:
            raise _unauthorized()
        if scope not in principal.scopes:
            raise _forbidden("This API key lacks the required scope.")
        request.state.principal = principal
        return principal

    return _mark(dep, Access("scope", scope.value))


def internal_only() -> Callable[[Request], None]:
    def dep(request: Request) -> None:
        expected = request.app.state.settings.internal_auth_secret.get_secret_value()
        presented = request.headers.get(INTERNAL_AUTH_HEADER, "")
        if not constant_time_equals(presented, expected):
            raise _unauthorized()

    return _mark(dep, Access("internal"))


def ingest_token() -> Callable[[Request], Awaitable[TokenInfo]]:
    """The bearer token must be a live ingest token of the integration in the path.

    Ingest tokens are write-only for telemetry; the only thing they can read is
    their own integration's agent config (which holds no secrets).
    """

    async def dep(request: Request) -> TokenInfo:
        gate: IngestGate = request.app.state.ingest_gate
        token = bearer_token(request.headers.get("Authorization"))
        info = await gate.authenticate(token) if token else None
        if info is None or str(info.integration_id) != request.path_params.get("integration_id"):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Authentication required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        retry = gate.limiter.hit(info.token_id, info.rate_limit_per_min)
        if retry is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Rate limit exceeded.",
                headers={"Retry-After": str(retry)},
            )
        return info

    return _mark(dep, Access("ingest"))


def actor_for(request: Request, principal: SessionPrincipal | KeyPrincipal | None) -> Actor:
    ip = client_ip(request)
    if isinstance(principal, SessionPrincipal):
        return Actor(ActorType.USER, str(principal.user.id), ip)
    if isinstance(principal, KeyPrincipal):
        return Actor(ActorType.API_KEY, str(principal.key.id), ip)
    return Actor(ActorType.SYSTEM, None, ip)


def route_access(dependant_calls: list[Callable[..., object]]) -> list[Access]:
    """Access markers found among a route's dependency callables (used by tests and docs)."""
    return [a for c in dependant_calls if (a := getattr(c, ACCESS_ATTR, None)) is not None]
