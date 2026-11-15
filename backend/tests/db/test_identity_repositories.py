"""CRUD for users, sessions and API keys through the repositories (as sentinel_app)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import User, UserRole
from sentinel.db.repositories import ApiKeyRepository, SessionRepository, UserRepository

pytestmark = pytest.mark.db

HASH_A = "a" * 64
HASH_B = "b" * 64
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FAKE_HASH = "x" * 32  # real Argon2id hashing arrives in Step 3


async def _user(s: AsyncSession, email: str = "alice@example.com") -> User:
    return await UserRepository(s).add(email=email, display_name="Alice", password_hash=FAKE_HASH)


async def test_user_create_and_defaults(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    await db_session.refresh(user)
    assert user.id.version == 7
    assert user.role is UserRole.VIEWER
    assert user.is_active is True
    assert user.failed_login_count == 0
    assert user.created_at is not None


async def test_email_lookup_is_case_insensitive(db_session: AsyncSession) -> None:
    user = await _user(db_session, "Bob@Example.com")
    found = await UserRepository(db_session).get_by_email("bob@EXAMPLE.com")
    assert found is not None
    assert found.id == user.id


async def test_email_unique_case_insensitive(db_session: AsyncSession) -> None:
    await _user(db_session, "carol@example.com")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _user(db_session, "CAROL@example.com")


async def test_user_list_count_role_and_active(db_session: AsyncSession) -> None:
    repo = UserRepository(db_session)
    u1 = await _user(db_session, "u1@example.com")
    await _user(db_session, "u2@example.com")
    assert await repo.count() == 2
    assert [u.email for u in await repo.list()] == ["u1@example.com", "u2@example.com"]

    await repo.set_role(u1.id, UserRole.APPROVER)
    await repo.set_active(u1.id, active=False)
    await db_session.refresh(u1)
    assert u1.role is UserRole.APPROVER
    assert u1.is_active is False


async def test_display_name_check_constraint(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await UserRepository(db_session).add(
                email="x@example.com", display_name="", password_hash="h"
            )


async def test_sessions_active_lookup_and_revoke(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    repo = SessionRepository(db_session)
    sess = await repo.add(
        user_id=user.id,
        token_hash=HASH_A,
        expires_at=NOW + timedelta(hours=12),
        ip="203.0.113.7",
        user_agent="pytest",
    )
    assert (await repo.get_active_by_token_hash(HASH_A, now=NOW)) is not None
    # Expired from the point of view of a later "now".
    assert (await repo.get_active_by_token_hash(HASH_A, now=NOW + timedelta(days=1))) is None

    await repo.revoke(sess.id, now=NOW)
    assert (await repo.get_active_by_token_hash(HASH_A, now=NOW)) is None


async def test_revoke_all_sessions_for_user(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    repo = SessionRepository(db_session)
    for h in (HASH_A, HASH_B):
        await repo.add(user_id=user.id, token_hash=h, expires_at=NOW + timedelta(hours=1))
    assert await repo.revoke_all_for_user(user.id, now=NOW) == 2
    assert await repo.revoke_all_for_user(user.id, now=NOW) == 0


async def test_session_token_hash_must_be_sha256_hex_length(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await SessionRepository(db_session).add(
                user_id=user.id, token_hash="short", expires_at=NOW
            )


async def test_deleting_user_cascades_sessions_and_nulls_api_key_creator(
    db_session: AsyncSession,
) -> None:
    user = await _user(db_session)
    await SessionRepository(db_session).add(
        user_id=user.id, token_hash=HASH_A, expires_at=NOW + timedelta(hours=1)
    )
    key = await ApiKeyRepository(db_session).add(
        name="alertmanager",
        prefix="sk_abcde",
        key_hash=HASH_B,
        scopes=["alerts:write"],
        created_by=user.id,
    )
    await db_session.execute(delete(User).where(User.id == user.id))
    assert (await SessionRepository(db_session).get_active_by_token_hash(HASH_A, now=NOW)) is None
    await db_session.refresh(key)
    assert key.created_by is None


async def test_api_keys_lifecycle(db_session: AsyncSession) -> None:
    repo = ApiKeyRepository(db_session)
    key = await repo.add(
        name="ci", prefix="sk_12345", key_hash=HASH_A, scopes=["deployments:write"], created_by=None
    )
    found = await repo.get_active_by_hash(HASH_A)
    assert found is not None
    assert found.scopes == ["deployments:write"]

    await repo.touch(key.id, now=NOW)
    await db_session.refresh(key)
    assert key.last_used_at == NOW

    await repo.revoke(key.id, now=NOW)
    assert await repo.get_active_by_hash(HASH_A) is None
    assert [k.name for k in await repo.list()] == ["ci"]
