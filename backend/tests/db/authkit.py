"""Helpers for API-level auth tests against the real (Testcontainers) database."""

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.api.app import create_app
from sentinel.api.security import API_KEY_HEADER, CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from sentinel.auth.service import Actor, AuthService
from sentinel.config import Environment, Settings
from sentinel.db.engine import create_session_factory, session_scope
from sentinel.db.models import UserRole

PASSWORD = "correct-horse-battery-staple"
BASE_URL = "https://testserver"  # https so Secure cookies are sent
# http.cookiejar stores cookies for a dot-less host under "<host>.local".
COOKIE_DOMAIN = "testserver.local"
CLIENT_IP = "203.0.113.10"  # TEST-NET-3 (RFC 5737)


def make_settings(db_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": Environment.TEST,
        "database_url": SecretStr(db_url),
        "secret_key": SecretStr("test-secret-key-for-auth-tests-0123456789"),
        "login_rate_limit_per_minute": 10_000,
        "background_jobs_enabled": False,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def unique_email(tag: str = "user") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.com"


def seed_user(
    settings: Settings,
    *,
    role: UserRole,
    email: str | None = None,
    password: str | None = PASSWORD,
    active: bool = True,
) -> str:
    """Create a user directly through the service (as the CLI would). Returns the email."""
    email = email or unique_email(role.value)

    async def run() -> None:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            async with session_scope(create_session_factory(engine)) as db:
                svc = AuthService(db, settings)
                user, _ = await svc.create_user(
                    email=email,
                    display_name=role.value.title(),
                    role=role,
                    password=password,
                    actor=Actor.system("test"),
                )
                if not active:
                    user.is_active = False
        finally:
            await engine.dispose()

    asyncio.run(run())
    return email


@contextmanager
def client_for(app: FastAPI) -> Iterator[TestClient]:
    # A real-looking peer address, so IP handling (INET columns, audit) is exercised.
    with TestClient(app, base_url=BASE_URL, client=(CLIENT_IP, 50000)) as client:
        yield client


def app_with(settings: Settings) -> FastAPI:
    return create_app(settings)


class Session:
    """Credentials for one logged-in user, applied per request."""

    def __init__(self, session_cookie: str, csrf: str) -> None:
        self.session_cookie = session_cookie
        self.csrf = csrf

    def apply(self, client: TestClient, *, with_csrf: bool = True) -> dict[str, str]:
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, self.session_cookie, domain=COOKIE_DOMAIN)
        client.cookies.set(CSRF_COOKIE, self.csrf, domain=COOKIE_DOMAIN)
        return {CSRF_HEADER: self.csrf} if with_csrf else {}


def login(client: TestClient, email: str, password: str = PASSWORD) -> Session:
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return Session(r.cookies[SESSION_COOKIE], r.json()["csrf_token"])


def as_key(client: TestClient, key: str) -> dict[str, str]:
    client.cookies.clear()
    return {API_KEY_HEADER: key}
