"""Authentication, sessions, CSRF, lockout and rate limiting through the real API."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.api.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from sentinel.auth import tokens
from sentinel.config import Settings
from sentinel.db.models import AuditLog, User, UserRole
from sentinel.db.models import Session as DbSession

from .authkit import (
    COOKIE_DOMAIN,
    PASSWORD,
    app_with,
    client_for,
    login,
    make_settings,
    seed_user,
    unique_email,
)
from .conftest import Pg

pytestmark = pytest.mark.db


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(migrated.url("sentinel_app"))


def _run[T](settings: Settings, fn) -> T:  # type: ignore[no-untyped-def]
    async def go() -> T:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)  # type: ignore[no-any-return]
        finally:
            await engine.dispose()

    return asyncio.run(go())


# ------------------------------------------------------------- happy path


def test_login_me_logout(settings: Settings) -> None:
    email = seed_user(settings, role=UserRole.RESPONDER)
    with client_for(app_with(settings)) as client:
        r = client.post("/api/v1/auth/login", json={"email": email.upper(), "password": PASSWORD})
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["email"] == email
        assert "incidents:write" in body["permissions"]
        assert "users:manage" not in body["permissions"]

        cookies = r.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
        csrf_cookie = next(c for c in cookies if c.startswith(f"{CSRF_COOKIE}="))
        for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            assert attr.lower() in session_cookie.lower()
        assert "httponly" not in csrf_cookie.lower()  # the SPA must be able to read it

        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.get("/api/v1/meta").json()["env"] == "test"

        # Logout is state-changing: CSRF header required.
        assert client.post("/api/v1/auth/logout").status_code == 403
        csrf = client.cookies[CSRF_COOKIE]
        assert client.post("/api/v1/auth/logout", headers={CSRF_HEADER: csrf}).status_code == 204

        # The old cookie no longer works, even if replayed.
        client.cookies.set(SESSION_COOKIE, r.cookies[SESSION_COOKIE], domain=COOKIE_DOMAIN)
        assert client.get("/api/v1/auth/me").status_code == 401


def test_session_token_is_stored_hashed(settings: Settings) -> None:
    email = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        s = login(client, email)

    async def stored(conn):  # type: ignore[no-untyped-def]
        return (await conn.execute(select(DbSession.token_hash))).scalars().all()

    hashes = _run(settings, stored)
    assert s.session_cookie not in hashes
    secret = settings.secret_key.get_secret_value()
    assert tokens.hash_token(secret, s.session_cookie, purpose="session") in hashes


# ------------------------------------------------------------------- CSRF


def test_csrf_missing_wrong_or_foreign_token_rejected(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    other = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        foreign = login(client, other)
        s = login(client, admin)
        body = {"email": unique_email(), "display_name": "X"}
        for headers in ({}, {CSRF_HEADER: "nope"}, {CSRF_HEADER: foreign.csrf}):
            s.apply(client, with_csrf=False)
            r = client.post("/api/v1/users", json=body, headers=headers)
            assert r.status_code == 403
            assert "CSRF" in r.json()["detail"]
        # Safe methods don't need it; the correct token works.
        s.apply(client, with_csrf=False)
        assert client.get("/api/v1/users").status_code == 200
        assert client.post("/api/v1/users", json=body, headers=s.apply(client)).status_code == 201


# ---------------------------------------------------- enumeration & lockout


def test_no_user_enumeration(settings: Settings) -> None:
    active = seed_user(settings, role=UserRole.VIEWER)
    inactive = seed_user(settings, role=UserRole.VIEWER, active=False)
    invited = seed_user(settings, role=UserRole.VIEWER, password=None)
    with client_for(app_with(settings)) as client:
        responses = [
            client.post("/api/v1/auth/login", json={"email": e, "password": p})
            for e, p in [
                (unique_email("ghost"), PASSWORD),
                (active, "wrong-password-123"),
                (inactive, PASSWORD),
                (invited, PASSWORD),
            ]
        ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1  # byte-identical bodies


def test_lockout_after_repeated_failures(settings: Settings) -> None:
    email = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        for _ in range(settings.lockout_threshold):
            bad = client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong-pw-xyz"}
            )
            assert bad.status_code == 401
        # Correct password is now refused too, with the same generic answer.
        r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 401

    async def check(conn):  # type: ignore[no-untyped-def]
        locked = (
            await conn.execute(select(User.locked_until).where(User.email == email))
        ).scalar_one()
        reasons = (
            await conn.execute(
                select(AuditLog.details["reason"].astext)
                .where(AuditLog.action == "auth.login_failed")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        ).scalar_one()
        return locked, reasons

    locked_until, last_reason = _run(settings, check)
    assert locked_until is not None
    assert locked_until > datetime.now(UTC)
    assert last_reason == "locked"


def test_lockout_expires(settings: Settings) -> None:
    email = seed_user(settings, role=UserRole.VIEWER)

    async def unlock_in_past(conn):  # type: ignore[no-untyped-def]
        await conn.execute(
            update(User)
            .where(User.email == email)
            .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )

    _run(settings, unlock_in_past)
    with client_for(app_with(settings)) as client:
        assert login(client, email)


def test_login_rate_limit(migrated: Pg) -> None:
    settings = make_settings(migrated.url("sentinel_app"), login_rate_limit_per_minute=3)
    with client_for(app_with(settings)) as client:
        codes = [
            client.post(
                "/api/v1/auth/login", json={"email": unique_email(), "password": "whatever-123"}
            )
            for _ in range(4)
        ]
    assert [r.status_code for r in codes] == [401, 401, 401, 429]
    assert int(codes[-1].headers["Retry-After"]) >= 1


# ------------------------------------------------------- session lifetime


def test_idle_session_expires(settings: Settings) -> None:
    email = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        s = login(client, email)
        secret = settings.secret_key.get_secret_value()
        token_hash = tokens.hash_token(secret, s.session_cookie, purpose="session")

        async def age(conn):  # type: ignore[no-untyped-def]
            await conn.execute(
                update(DbSession)
                .where(DbSession.token_hash == token_hash)
                .values(last_seen_at=datetime.now(UTC) - timedelta(hours=13))
            )

        _run(settings, age)
        s.apply(client)
        assert client.get("/api/v1/auth/me").status_code == 401


def test_deactivation_and_role_change_revoke_sessions(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    victim = seed_user(settings, role=UserRole.RESPONDER)
    with client_for(app_with(settings)) as client:
        v = login(client, victim)
        a = login(client, admin)
        users = client.get("/api/v1/users").json()
        victim_id = next(u["id"] for u in users if u["email"] == victim)

        r = client.patch(
            f"/api/v1/users/{victim_id}", json={"role": "viewer"}, headers=a.apply(client)
        )
        assert r.status_code == 200
        assert r.json()["role"] == "viewer"
        v.apply(client)
        assert client.get("/api/v1/auth/me").status_code == 401

        v = login(client, victim)
        r = client.patch(
            f"/api/v1/users/{victim_id}", json={"is_active": False}, headers=a.apply(client)
        )
        assert r.status_code == 200
        v.apply(client)
        assert client.get("/api/v1/auth/me").status_code == 401
        r = client.post("/api/v1/auth/login", json={"email": victim, "password": PASSWORD})
        assert r.status_code == 401


def test_password_hash_never_leaves_the_api(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        login(client, admin)
        body = client.get("/api/v1/users").text
    assert "argon2" not in body
    assert "password_hash" not in body


def test_superuser_connection_sanity(migrated: Pg) -> None:
    """The privileged test connection exists (used by the tamper test)."""

    async def go() -> int:
        engine = create_async_engine(migrated.url("postgres"), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return int((await conn.execute(text("SELECT 1"))).scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == 1
