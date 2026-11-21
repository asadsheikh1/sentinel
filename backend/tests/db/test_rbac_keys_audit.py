"""Route x role matrix, API keys and scopes, invites, and the audit hash chain."""

import asyncio
import uuid

import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.api.rbac_docs import route_table
from sentinel.api.security import require_scope
from sentinel.auth.permissions import ROLE_PERMISSIONS, KeyScope, Permission
from sentinel.auth.service import Actor, AuthService, ConflictError
from sentinel.cli import main as cli_main
from sentinel.config import Settings, get_settings
from sentinel.db import migrate
from sentinel.db.models import UserRole
from sentinel.security import audit

from .authkit import (
    PASSWORD,
    Session,
    app_with,
    as_key,
    client_for,
    login,
    make_settings,
    seed_user,
    unique_email,
)
from .conftest import Pg

pytestmark = pytest.mark.db

ROLES = [UserRole.VIEWER, UserRole.RESPONDER, UserRole.APPROVER, UserRole.ADMIN]


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(migrated.url("sentinel_app"))


def _concrete(path: str) -> str:
    """Fill path parameters with a random UUID (the resource won't exist; that's fine)."""
    parts = [str(uuid.uuid4()) if p.startswith("{") else p for p in path.split("/")]
    return "/".join(parts)


# ------------------------------------------------------- route x role matrix


def test_route_role_matrix(settings: Settings) -> None:
    """Every route, every role: anonymous → 401, missing permission → 403, else allowed."""
    emails = {role: seed_user(settings, role=role) for role in ROLES}
    app = app_with(settings)
    table = route_table(app)
    checked = 0
    with client_for(app) as client:
        sessions: dict[UserRole, Session] = {r: login(client, e) for r, e in emails.items()}
        for route in table:
            access = route.access[0]
            if access.kind == "public":
                continue
            path = _concrete(route.path)

            client.cookies.clear()
            r = client.request(route.method, path, json={})
            assert r.status_code == 401, f"anonymous {route.method} {route.path} → {r.status_code}"

            for role in ROLES:
                if access.kind in ("scope", "internal", "ingest", "prober"):
                    expected_denied = True  # user sessions never satisfy these
                elif access.kind == "authenticated":
                    expected_denied = False
                else:
                    expected_denied = Permission(str(access.value)) not in ROLE_PERMISSIONS[role]
                headers = sessions[role].apply(client)
                r = client.request(route.method, path, json={}, headers=headers)
                label = f"{role.value} {route.method} {route.path} → {r.status_code}"
                if expected_denied:
                    assert r.status_code in (401, 403), label
                else:
                    assert r.status_code not in (401, 403), label
                checked += 1
                if route.path == "/api/v1/auth/logout":
                    sessions[role] = login(client, emails[role])  # that call ended the session
    protected = [r for r in table if r.access[0].kind != "public"]
    assert len(protected) >= 12
    assert checked == len(protected) * len(ROLES)


# ------------------------------------------------------------- API keys


def test_api_key_lifecycle_and_scopes(settings: Settings) -> None:
    app = app_with(settings)
    probe = APIRouter()

    @probe.post("/api/v1/test/alerts", dependencies=[Depends(require_scope(KeyScope.ALERTS_WRITE))])
    async def alerts_probe() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(probe)
    admin = seed_user(settings, role=UserRole.ADMIN)

    with client_for(app) as client:
        a = login(client, admin)
        r = client.post(
            "/api/v1/api-keys",
            json={"name": "alertmanager", "scopes": ["alerts:write"]},
            headers=a.apply(client),
        )
        assert r.status_code == 201
        good_key = r.json()["secret"]
        good_id = r.json()["api_key"]["id"]
        assert good_key.startswith("sk_live_")
        r = client.post(
            "/api/v1/api-keys",
            json={"name": "ci", "scopes": ["deployments:write"]},
            headers=a.apply(client),
        )
        wrong_scope_key = r.json()["secret"]

        # The secret is shown once: listing never includes it.
        listing = client.get("/api/v1/api-keys").text
        assert good_key not in listing
        assert good_key.split("_")[2] in listing  # the display prefix is there

        url = "/api/v1/test/alerts"
        client.cookies.clear()
        assert client.post(url).status_code == 401
        assert client.post(url, headers=as_key(client, "sk_live_bogus")).status_code == 401
        forged = good_key[:-4] + "AAAA"
        assert client.post(url, headers=as_key(client, forged)).status_code == 401
        assert client.post(url, headers=as_key(client, wrong_scope_key)).status_code == 403
        assert client.post(url, headers=as_key(client, good_key)).status_code == 200

        # A key never acts as a user.
        assert client.get("/api/v1/users", headers=as_key(client, good_key)).status_code == 401

        # last_used_at recorded; revocation is immediate.
        a.apply(client)
        used = next(k for k in client.get("/api/v1/api-keys").json() if k["id"] == good_id)
        assert used["last_used_at"] is not None
        assert (
            client.delete(f"/api/v1/api-keys/{good_id}", headers=a.apply(client)).status_code == 204
        )
        assert (
            client.delete(f"/api/v1/api-keys/{good_id}", headers=a.apply(client)).status_code == 404
        )
        assert client.post(url, headers=as_key(client, good_key)).status_code == 401


def test_api_key_rejects_unknown_scope(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        r = client.post(
            "/api/v1/api-keys",
            json={"name": "x", "scopes": ["users:manage"]},
            headers=a.apply(client),
        )
    assert r.status_code == 422


# --------------------------------------------------------------- invites


def test_invite_flow(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    new_email = unique_email("invited")
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        r = client.post(
            "/api/v1/users",
            json={"email": new_email, "display_name": "New", "role": "responder"},
            headers=a.apply(client),
        )
        assert r.status_code == 201
        token = r.json()["invite_token"]
        assert r.json()["user"]["has_password"] is False
        assert r.json()["invite_path"].startswith("/accept-invite#token=")

        dup = client.post(
            "/api/v1/users",
            json={"email": new_email.upper(), "display_name": "Dup"},
            headers=a.apply(client),
        )
        assert dup.status_code == 409

        client.cookies.clear()
        bad = client.post("/api/v1/auth/login", json={"email": new_email, "password": PASSWORD})
        assert bad.status_code == 401  # no password yet

        weak = client.post("/api/v1/auth/accept-invite", json={"token": token, "password": "short"})
        assert weak.status_code == 422
        ok = client.post("/api/v1/auth/accept-invite", json={"token": token, "password": PASSWORD})
        assert ok.status_code == 204
        again = client.post(
            "/api/v1/auth/accept-invite", json={"token": token, "password": PASSWORD + "x"}
        )
        assert again.status_code == 400  # one-time use

        assert login(client, new_email)

        # Re-issuing (e.g. password reset) invalidates older links.
        a.apply(client)
        user_id = next(
            u["id"] for u in client.get("/api/v1/users").json() if u["email"] == new_email
        )
        first = client.post(f"/api/v1/users/{user_id}/invite", headers=a.apply(client)).json()
        second = client.post(f"/api/v1/users/{user_id}/invite", headers=a.apply(client)).json()
        client.cookies.clear()
        stale = client.post(
            "/api/v1/auth/accept-invite",
            json={"token": first["invite_token"], "password": PASSWORD},
        )
        assert stale.status_code == 400
        fresh = client.post(
            "/api/v1/auth/accept-invite",
            json={"token": second["invite_token"], "password": "brand-new-password-1"},
        )
        assert fresh.status_code == 204
        assert login(client, new_email, "brand-new-password-1")


# ----------------------------------------------------- last-admin guard


def test_last_active_admin_cannot_be_removed(
    pg: Pg, fresh_database: str, settings: Settings
) -> None:
    """Run in an isolated database so other tests' admins don't count."""
    url = pg.url("sentinel_migrator", fresh_database)
    migrate.upgrade(settings, "head", url=url)
    app_url = pg.url("sentinel_app", fresh_database)
    s = make_settings(app_url)

    async def go() -> None:
        engine = create_async_engine(app_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                svc = AuthService(db, s)
                actor = Actor.system("test")
                only, _ = await svc.create_user(
                    email="root@example.com",
                    display_name="Root",
                    role=UserRole.ADMIN,
                    password=PASSWORD,
                    actor=actor,
                )
                with pytest.raises(ConflictError):
                    await svc.update_user(only, actor=actor, role=UserRole.VIEWER)
                with pytest.raises(ConflictError):
                    await svc.update_user(only, actor=actor, is_active=False)
                await svc.create_user(
                    email="second@example.com",
                    display_name="Second",
                    role=UserRole.ADMIN,
                    password=PASSWORD,
                    actor=actor,
                )
                await svc.update_user(only, actor=actor, role=UserRole.VIEWER)  # now allowed
                assert only.role is UserRole.VIEWER
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(go())


# ------------------------------------------------------------ audit log


def test_audit_trail_records_actions_and_chain_verifies(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        client.post(
            "/api/v1/api-keys",
            json={"name": "k", "scopes": ["alerts:write"]},
            headers=a.apply(client),
        )
        page = client.get("/api/v1/audit", params={"action": "apikey.", "limit": 1}).json()
        assert page["items"][0]["action"] == "apikey.create"
        assert page["items"][0]["details"]["name"] == "k"
        assert "secret" not in str(page["items"][0]["details"]).lower()
        assert page["next_cursor"] is None or isinstance(page["next_cursor"], str)

        everything = client.get("/api/v1/audit", params={"limit": 2}).json()
        assert everything["next_cursor"]
        nxt = client.get(
            "/api/v1/audit", params={"limit": 2, "cursor": everything["next_cursor"]}
        ).json()
        assert nxt["items"][0]["id"] < everything["items"][-1]["id"]

        logins = client.get("/api/v1/audit", params={"action": "auth.login"}).json()["items"]
        assert any(e["actor_type"] == "user" for e in logins)
        assert any(e["ip"] == "203.0.113.10" for e in logins)  # INET round-trip

        report = client.get("/api/v1/audit/verify").json()
        assert report["ok"] is True
        assert report["checked"] == report["total"] > 0


def test_tampering_is_detected(migrated: Pg) -> None:
    """A superuser bypasses grants and the trigger and edits a row → verify finds it.

    Everything happens in one transaction that is rolled back, so the shared
    test database stays intact.
    """

    async def go() -> audit.ChainReport:
        engine = create_async_engine(migrated.url("postgres"), poolclass=NullPool)
        try:
            async with AsyncSession(engine) as db:
                await audit.append(db, actor_type=audit.ActorType.SYSTEM, action="tamper.target")
                await audit.append(db, actor_type=audit.ActorType.SYSTEM, action="tamper.after")
                await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
                await db.execute(
                    text(
                        "UPDATE audit_log SET details = '{\"forged\": true}' "
                        "WHERE id = (SELECT id FROM audit_log WHERE action = 'tamper.target' "
                        "ORDER BY id DESC LIMIT 1)"
                    )
                )
                report = await audit.verify_chain(db)
                await db.rollback()
                return report
        finally:
            await engine.dispose()

    report = asyncio.run(go())
    assert report.ok is False
    assert report.reason == "content does not match hash"
    assert report.first_bad_id is not None


def test_deleted_row_is_detected(migrated: Pg) -> None:
    async def go() -> audit.ChainReport:
        engine = create_async_engine(migrated.url("postgres"), poolclass=NullPool)
        try:
            async with AsyncSession(engine) as db:
                first = await audit.append(db, actor_type=audit.ActorType.SYSTEM, action="d.1")
                await audit.append(db, actor_type=audit.ActorType.SYSTEM, action="d.2")
                await db.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
                await db.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": first.id})
                report = await audit.verify_chain(db)
                await db.rollback()
                return report
        finally:
            await engine.dispose()

    report = asyncio.run(go())
    assert report.ok is False
    assert report.reason == "prev_hash does not link"


# ------------------------------------------------------------------ CLI


def test_create_admin_cli(
    migrated: Pg,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    email = unique_email("cli-admin")
    monkeypatch.setenv("SENTINEL_DATABASE_URL", migrated.url("sentinel_app"))
    monkeypatch.setenv("SENTINEL_SECRET_KEY", settings.secret_key.get_secret_value())
    get_settings.cache_clear()
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        args = ["create-admin", "--email", email, "--name", "CLI Admin", "--password-stdin"]
        assert cli_main(args) == 0
        assert "Created admin" in capsys.readouterr().out

        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        assert cli_main(args) == 1  # duplicate
        assert "already exists" in capsys.readouterr().out

        monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
        args2 = ["create-admin", "--email", unique_email(), "--name", "X", "--password-stdin"]
        assert cli_main(args2) == 1
    finally:
        get_settings.cache_clear()

    with client_for(app_with(settings)) as client:
        s = login(client, email)
        s.apply(client)
        me = client.get("/api/v1/auth/me").json()
    assert me["user"]["role"] == "admin"
