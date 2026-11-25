"""Request and response bodies for the auth, users, API-key and audit endpoints."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from sentinel.auth.permissions import KeyScope, Permission
from sentinel.db.models import ActorType, UserRole


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(_Out):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    has_password: bool
    last_login_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, user: Any) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            has_password=user.password_hash is not None,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
        )


class MeOut(BaseModel):
    user: UserOut
    permissions: list[Permission]
    csrf_token: str


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    role: UserRole = UserRole.VIEWER


class UserCreated(BaseModel):
    user: UserOut
    invite_token: str = Field(description="Shown once. Send it to the user out of band.")
    invite_path: str


class UserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


class InviteOut(BaseModel):
    invite_token: str
    invite_path: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[KeyScope] = Field(min_length=1)


class ApiKeyOut(_Out):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(BaseModel):
    api_key: ApiKeyOut
    secret: str = Field(description="Shown once. Store it in your secret manager now.")


class AuditEntryOut(_Out):
    id: int
    ts: datetime
    actor_type: ActorType
    actor_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip: str | None = None
    hash: str

    @field_validator("ip", mode="before")
    @classmethod
    def _ip_to_str(cls, v: object) -> str | None:
        return None if v is None else str(v)  # Postgres INET → IPv4Address/IPv6Address


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None


class AuditVerifyOut(BaseModel):
    ok: bool
    checked: int
    total: int
    first_bad_id: int | None
    reason: str | None


class MetaOut(BaseModel):
    version: str
    env: str
    ingest_endpoint: str = Field(
        description="Where agents and apps send telemetry (OTLP over HTTP, with an ingest token)."
    )
