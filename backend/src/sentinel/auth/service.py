"""Authentication business rules: login, sessions, invites, API keys, user admin.

Every security-relevant change writes an audit event in the same transaction
as the change itself, so an action can't happen without its record.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.auth import passwords, tokens
from sentinel.auth.permissions import KeyScope, Permission, permissions_for
from sentinel.config import Settings
from sentinel.db.models import ActorType, ApiKey, Session, User, UserRole
from sentinel.db.repositories import (
    ApiKeyRepository,
    InviteRepository,
    SessionRepository,
    UserRepository,
)
from sentinel.security import audit

SESSION_TOUCH_INTERVAL = timedelta(minutes=1)


class InviteError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    """Who is performing an action (for audit records)."""

    type: ActorType
    id: str | None
    ip: str | None = None

    @classmethod
    def system(cls, name: str) -> "Actor":
        return cls(ActorType.SYSTEM, name)


@dataclass(frozen=True)
class LoginResult:
    user: User
    session_token: str  # goes in the HttpOnly cookie, never stored in plaintext
    csrf_token: str


@dataclass(frozen=True)
class SessionPrincipal:
    user: User
    session: Session
    permissions: frozenset[Permission]


@dataclass(frozen=True)
class KeyPrincipal:
    key: ApiKey
    scopes: frozenset[KeyScope]


def _now() -> datetime:
    return datetime.now(UTC)


class AuthService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.users = UserRepository(db)
        self.sessions = SessionRepository(db)
        self.keys = ApiKeyRepository(db)
        self.invites = InviteRepository(db)
        self._secret = settings.secret_key.get_secret_value()

    def _hash(self, token: str, purpose: str) -> str:
        return tokens.hash_token(self._secret, token, purpose=purpose)

    # ------------------------------------------------------------------ login

    async def login(
        self, email: str, password: str, *, ip: str | None, user_agent: str | None
    ) -> LoginResult | None:
        """Return a new session, or None. Callers must COMMIT even on None so the
        failure counter, lockout and audit record persist. The reason is never
        returned (no user enumeration); it is only in the audit log."""
        now = _now()
        user = await self.users.get_by_email(email)
        # Always run the expensive hash check, even for unknown emails.
        password_ok = passwords.verify_password(user.password_hash if user else None, password)

        failure: str | None = None
        if user is None:
            failure = "unknown_email"
        elif not user.is_active:
            failure = "inactive"
        elif user.locked_until is not None and user.locked_until > now:
            failure = "locked"
        elif not password_ok:
            failure = "bad_password"
            await self.users.record_login_failure(
                user,
                now=now,
                threshold=self.settings.lockout_threshold,
                lockout=timedelta(minutes=self.settings.lockout_minutes),
            )

        if failure is not None or user is None:
            await audit.append(
                self.db,
                actor_type=ActorType.USER if user else ActorType.SYSTEM,
                actor_id=str(user.id) if user else None,
                action="auth.login_failed",
                details={"reason": failure, "email": email if user is None else None},
                ip=ip,
            )
            return None

        if user.password_hash is not None and passwords.needs_rehash(user.password_hash):
            await self.users.set_password_hash(user, passwords.hash_password(password))
        await self.users.record_login_success(user, now=now)

        token = tokens.new_token()
        token_hash = self._hash(token, "session")
        await self.sessions.add(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=now + timedelta(days=self.settings.session_absolute_timeout_days),
            ip=ip,
            user_agent=(user_agent or "")[:500] or None,
        )
        await audit.append(
            self.db,
            actor_type=ActorType.USER,
            actor_id=str(user.id),
            action="auth.login",
            ip=ip,
        )
        return LoginResult(
            user=user,
            session_token=token,
            csrf_token=tokens.csrf_token_for(self._secret, token_hash),
        )

    async def authenticate_session(self, token: str) -> SessionPrincipal | None:
        now = _now()
        session = await self.sessions.get_active_by_token_hash(
            self._hash(token, "session"), now=now
        )
        if session is None:
            return None
        idle = timedelta(minutes=self.settings.session_idle_timeout_minutes)
        if session.last_seen_at + idle < now:
            await self.sessions.revoke(session.id, now=now)
            return None
        user = await self.users.get(session.user_id)
        if user is None or not user.is_active:
            return None
        if now - session.last_seen_at > SESSION_TOUCH_INTERVAL:
            await self.sessions.touch(session.id, now=now)
        return SessionPrincipal(user=user, session=session, permissions=permissions_for(user.role))

    def csrf_token(self, session: Session) -> str:
        return tokens.csrf_token_for(self._secret, session.token_hash)

    def csrf_valid(self, session: Session, presented: str | None) -> bool:
        return presented is not None and tokens.constant_time_equals(
            self.csrf_token(session), presented
        )

    async def logout(self, principal: SessionPrincipal, actor: Actor) -> None:
        await self.sessions.revoke(principal.session.id, now=_now())
        await audit.append(
            self.db, actor_type=actor.type, actor_id=actor.id, action="auth.logout", ip=actor.ip
        )

    # --------------------------------------------------------------- api keys

    async def authenticate_key(self, plaintext: str) -> KeyPrincipal | None:
        if tokens.parse_api_key(plaintext) is None:
            return None
        key = await self.keys.get_active_by_hash(self._hash(plaintext, "api_key"))
        if key is None:
            return None
        await self.keys.touch(key.id, now=_now())
        scopes = frozenset(KeyScope(s) for s in key.scopes if s in KeyScope._value2member_map_)
        return KeyPrincipal(key=key, scopes=scopes)

    async def create_api_key(
        self, *, name: str, scopes: list[KeyScope], actor: Actor, created_by: uuid.UUID | None
    ) -> tuple[ApiKey, str]:
        new = tokens.new_api_key()
        key = await self.keys.add(
            name=name,
            prefix=new.prefix,
            key_hash=self._hash(new.plaintext, "api_key"),
            scopes=sorted(s.value for s in set(scopes)),
            created_by=created_by,
        )
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action="apikey.create",
            target_type="api_key",
            target_id=str(key.id),
            details={"name": name, "scopes": key.scopes, "prefix": key.prefix},
            ip=actor.ip,
        )
        return key, new.plaintext

    async def revoke_api_key(self, key_id: uuid.UUID, actor: Actor) -> bool:
        key = await self.db.get(ApiKey, key_id)
        if key is None or key.revoked_at is not None:
            return False
        await self.keys.revoke(key_id, now=_now())
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action="apikey.revoke",
            target_type="api_key",
            target_id=str(key_id),
            ip=actor.ip,
        )
        return True

    # ------------------------------------------------------------------ users

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        role: UserRole,
        actor: Actor,
        password: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> tuple[User, str | None]:
        """Create a user. Without ``password`` an invite token is returned (shown once)."""
        if await self.users.get_by_email(email) is not None:
            raise ConflictError("A user with this email already exists.")
        password_hash = None
        if password is not None:
            passwords.validate_password(password, min_length=self.settings.password_min_length)
            password_hash = passwords.hash_password(password)
        user = await self.users.add(
            email=email, display_name=display_name, password_hash=password_hash, role=role
        )
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action="user.create",
            target_type="user",
            target_id=str(user.id),
            details={"email": email, "role": role.value, "invited": password is None},
            ip=actor.ip,
        )
        invite_token = None
        if password is None:
            invite_token = await self.issue_invite(user, actor=actor, created_by=created_by)
        return user, invite_token

    async def issue_invite(
        self, user: User, *, actor: Actor, created_by: uuid.UUID | None = None
    ) -> str:
        now = _now()
        await self.invites.invalidate_for_user(user.id, now=now)
        token = tokens.new_token()
        await self.invites.add(
            user_id=user.id,
            token_hash=self._hash(token, "invite"),
            expires_at=now + timedelta(hours=self.settings.invite_ttl_hours),
            created_by=created_by,
        )
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action="user.invite",
            target_type="user",
            target_id=str(user.id),
            ip=actor.ip,
        )
        return token

    async def accept_invite(self, token: str, password: str, *, ip: str | None) -> User:
        now = _now()
        invite = await self.invites.get_usable(self._hash(token, "invite"), now=now)
        if invite is None:
            raise InviteError("This invite link is invalid or has expired.")
        user = await self.users.get(invite.user_id)
        if user is None or not user.is_active:
            raise InviteError("This invite link is invalid or has expired.")
        passwords.validate_password(password, min_length=self.settings.password_min_length)
        await self.users.set_password_hash(user, passwords.hash_password(password))
        invite.used_at = now
        await audit.append(
            self.db,
            actor_type=ActorType.USER,
            actor_id=str(user.id),
            action="user.invite_accepted",
            target_type="user",
            target_id=str(user.id),
            ip=ip,
        )
        return user

    async def update_user(
        self,
        user: User,
        *,
        actor: Actor,
        role: UserRole | None = None,
        is_active: bool | None = None,
        display_name: str | None = None,
    ) -> User:
        changes: dict[str, object] = {}
        removing_admin = (
            user.role is UserRole.ADMIN
            and user.is_active
            and ((role is not None and role is not UserRole.ADMIN) or is_active is False)
        )
        if removing_admin and await self.users.count_active_admins() <= 1:
            raise ConflictError("Cannot remove or deactivate the last active admin.")

        if role is not None and role is not user.role:
            changes["role"] = {"from": user.role.value, "to": role.value}
            user.role = role
        if is_active is not None and is_active is not user.is_active:
            changes["is_active"] = {"from": user.is_active, "to": is_active}
            user.is_active = is_active
        if display_name is not None and display_name != user.display_name:
            changes["display_name"] = {"from": user.display_name, "to": display_name}
            user.display_name = display_name
        if not changes:
            return user
        await self.db.flush()

        # Losing privileges or access must take effect immediately.
        if "role" in changes or is_active is False:
            await self.sessions.revoke_all_for_user(user.id, now=_now())
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action="user.update",
            target_type="user",
            target_id=str(user.id),
            details=changes,
            ip=actor.ip,
        )
        return user
