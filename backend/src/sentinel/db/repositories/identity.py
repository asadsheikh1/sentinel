"""Data access for users, sessions and API keys.

Repositories take an ``AsyncSession`` and never commit: the caller owns the
transaction (see ``sentinel.db.engine.session_scope``). Business rules such as
password hashing and lockout live in the auth service (Step 3), not here.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import ApiKey, Session, User, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        role: UserRole = UserRole.VIEWER,
    ) -> User:
        user = User(email=email, display_name=display_name, password_hash=password_hash, role=role)
        self._s.add(user)
        await self._s.flush()
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self._s.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        # `email` is CITEXT, so this match is case-insensitive.
        return await self._s.scalar(select(User).where(User.email == email))

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[User]:
        result = await self._s.scalars(
            select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
        )
        return result.all()

    async def count(self) -> int:
        return int(await self._s.scalar(select(func.count()).select_from(User)) or 0)

    async def set_role(self, user_id: uuid.UUID, role: UserRole) -> None:
        await self._s.execute(update(User).where(User.id == user_id).values(role=role))

    async def set_active(self, user_id: uuid.UUID, *, active: bool) -> None:
        await self._s.execute(update(User).where(User.id == user_id).values(is_active=active))


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> Session:
        row = Session(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            ip=ip,
            user_agent=user_agent,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get_active_by_token_hash(self, token_hash: str, *, now: datetime) -> Session | None:
        return await self._s.scalar(
            select(Session).where(
                Session.token_hash == token_hash,
                Session.revoked_at.is_(None),
                Session.expires_at > now,
            )
        )

    async def revoke(self, session_id: uuid.UUID, *, now: datetime) -> None:
        await self._s.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        result = await self._s.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class ApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        name: str,
        prefix: str,
        key_hash: str,
        scopes: list[str],
        created_by: uuid.UUID | None,
    ) -> ApiKey:
        key = ApiKey(
            name=name, prefix=prefix, key_hash=key_hash, scopes=scopes, created_by=created_by
        )
        self._s.add(key)
        await self._s.flush()
        return key

    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None:
        return await self._s.scalar(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )

    async def list(self) -> Sequence[ApiKey]:
        return (await self._s.scalars(select(ApiKey).order_by(ApiKey.created_at))).all()

    async def touch(self, key_id: uuid.UUID, *, now: datetime) -> None:
        await self._s.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now))

    async def revoke(self, key_id: uuid.UUID, *, now: datetime) -> None:
        await self._s.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=now)
        )
