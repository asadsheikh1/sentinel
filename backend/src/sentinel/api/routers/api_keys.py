"""/api/v1/api-keys: machine keys for webhooks and CI (secret shown once)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from sentinel.api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from sentinel.api.security import AuthSvc, actor_for, require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import SessionPrincipal

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])
KeyAdmin = Annotated[SessionPrincipal, Depends(require(Permission.APIKEYS_MANAGE))]


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(svc: AuthSvc, _: KeyAdmin) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in await svc.keys.list()]


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate, request: Request, svc: AuthSvc, admin: KeyAdmin
) -> ApiKeyCreated:
    key, secret = await svc.create_api_key(
        name=body.name,
        scopes=body.scopes,
        actor=actor_for(request, admin),
        created_by=admin.user.id,
    )
    return ApiKeyCreated(api_key=ApiKeyOut.model_validate(key), secret=secret)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID, request: Request, svc: AuthSvc, admin: KeyAdmin
) -> Response:
    if not await svc.revoke_api_key(key_id, actor_for(request, admin)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found or already revoked.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
