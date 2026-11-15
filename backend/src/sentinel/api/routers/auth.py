"""/api/v1/auth: login, logout, current user, invite acceptance."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from sentinel.api.deps import DbSession
from sentinel.api.schemas import AcceptInviteRequest, LoginRequest, MeOut, UserOut
from sentinel.api.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthSvc,
    actor_for,
    authenticated,
    client_ip,
    public,
)
from sentinel.auth.passwords import PasswordPolicyError
from sentinel.auth.service import InviteError, SessionPrincipal

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

INVALID_LOGIN = "Invalid email or password."


def _rate_limit(request: Request, *keys: str) -> None:
    limiter = request.app.state.login_limiter
    for key in keys:
        retry = limiter.hit(key)
        if retry is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many attempts. Try again later.",
                headers={"Retry-After": str(int(retry) + 1)},
            )


def _set_auth_cookies(request: Request, response: Response, session: str, csrf: str) -> None:
    s = request.app.state.settings
    max_age = s.session_absolute_timeout_days * 86400
    common = {"secure": s.cookie_secure, "samesite": "strict", "path": "/", "max_age": max_age}
    response.set_cookie(SESSION_COOKIE, session, httponly=True, **common)
    # Readable by the SPA so it can echo it in X-CSRF-Token.
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)


def _clear_auth_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


@router.post("/login", response_model=MeOut, dependencies=[Depends(public())])
async def login(
    body: LoginRequest, request: Request, response: Response, svc: AuthSvc, db: DbSession
) -> MeOut:
    """Email + password → session cookie. Same 401 for every failure (no enumeration)."""
    ip = client_ip(request) or "unknown"
    _rate_limit(request, f"ip:{ip}", f"email:{body.email.lower()}")
    result = await svc.login(
        body.email,
        body.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if result is None:
        await db.commit()  # keep the failure counter, lockout and audit record
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_LOGIN)
    _set_auth_cookies(request, response, result.session_token, result.csrf_token)
    from sentinel.auth.permissions import permissions_for

    return MeOut(
        user=UserOut.of(result.user),
        permissions=sorted(permissions_for(result.user.role)),
        csrf_token=result.csrf_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    svc: AuthSvc,
    principal: Annotated[SessionPrincipal, Depends(authenticated())],
) -> Response:
    await svc.logout(principal, actor_for(request, principal))
    response.status_code = status.HTTP_204_NO_CONTENT
    _clear_auth_cookies(response)
    return response


@router.get("/me", response_model=MeOut)
async def me(
    svc: AuthSvc, principal: Annotated[SessionPrincipal, Depends(authenticated())]
) -> MeOut:
    return MeOut(
        user=UserOut.of(principal.user),
        permissions=sorted(principal.permissions),
        csrf_token=svc.csrf_token(principal.session),
    )


@router.post(
    "/accept-invite", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(public())]
)
async def accept_invite(body: AcceptInviteRequest, request: Request, svc: AuthSvc) -> Response:
    """Set a password using a one-time invite token. Then log in normally."""
    _rate_limit(request, f"ip:{client_ip(request) or 'unknown'}")
    try:
        await svc.accept_invite(body.token, body.password, ip=client_ip(request))
    except InviteError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except PasswordPolicyError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
