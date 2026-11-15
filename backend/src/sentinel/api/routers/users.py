"""/api/v1/users: user administration (admins only; no public sign-up)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from sentinel.api.schemas import InviteOut, UserCreate, UserCreated, UserOut, UserUpdate
from sentinel.api.security import AuthSvc, actor_for, require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import ConflictError, SessionPrincipal
from sentinel.db.models import User

router = APIRouter(prefix="/api/v1/users", tags=["users"])
Admin = Annotated[SessionPrincipal, Depends(require(Permission.USERS_MANAGE))]


def _invite_path(token: str) -> str:
    return f"/accept-invite#token={token}"  # fragment: never sent to servers or logs


async def _get_user(svc: AuthSvc, user_id: uuid.UUID) -> User:
    user = await svc.users.get(user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    return user


@router.get("", response_model=list[UserOut])
async def list_users(svc: AuthSvc, _: Admin) -> list[UserOut]:
    return [UserOut.of(u) for u in await svc.users.list(limit=500)]


@router.post("", response_model=UserCreated, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, request: Request, svc: AuthSvc, admin: Admin
) -> UserCreated:
    """Create a user and return a one-time invite token for them to set a password."""
    try:
        user, token = await svc.create_user(
            email=body.email,
            display_name=body.display_name,
            role=body.role,
            actor=actor_for(request, admin),
            created_by=admin.user.id,
        )
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    if token is None:  # pragma: no cover - create_user always invites without a password
        raise RuntimeError("invite token missing")
    return UserCreated(user=UserOut.of(user), invite_token=token, invite_path=_invite_path(token))


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, request: Request, svc: AuthSvc, admin: Admin
) -> UserOut:
    """Change role, activate/deactivate, or rename. Revokes the user's sessions on
    role change or deactivation. The last active admin can't be removed."""
    user = await _get_user(svc, user_id)
    try:
        await svc.update_user(
            user,
            actor=actor_for(request, admin),
            role=body.role,
            is_active=body.is_active,
            display_name=body.display_name,
        )
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return UserOut.of(user)


@router.post("/{user_id}/invite", response_model=InviteOut)
async def reissue_invite(
    user_id: uuid.UUID, request: Request, svc: AuthSvc, admin: Admin
) -> InviteOut:
    """Issue a fresh invite (older ones stop working), e.g. for a password reset."""
    user = await _get_user(svc, user_id)
    token = await svc.issue_invite(user, actor=actor_for(request, admin), created_by=admin.user.id)
    return InviteOut(invite_token=token, invite_path=_invite_path(token))
