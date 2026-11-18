"""Auth building blocks that need no database."""

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sentinel.api.app import create_app
from sentinel.api.rbac_docs import render, route_table
from sentinel.auth import passwords, tokens
from sentinel.auth.permissions import ROLE_PERMISSIONS, Permission
from sentinel.auth.ratelimit import RateLimiter
from sentinel.config import Environment, Settings
from sentinel.db.models import UserRole
from sentinel.security import audit

SECRET = "unit-test-secret-key-0123456789abcdef"


def _app():  # type: ignore[no-untyped-def]
    return create_app(Settings(env=Environment.TEST, _env_file=None))  # type: ignore[call-arg]


# ------------------------------------------------------------------ roles


def test_roles_are_strictly_nested() -> None:
    order = [UserRole.VIEWER, UserRole.RESPONDER, UserRole.APPROVER, UserRole.ADMIN]
    for lower, higher in itertools.pairwise(order):
        assert ROLE_PERMISSIONS[lower] < ROLE_PERMISSIONS[higher]
    assert ROLE_PERMISSIONS[UserRole.ADMIN] == frozenset(Permission)


def test_matrix_matches_security_doc() -> None:
    """Spot-check the design table in docs/2-architecture/6-security.md."""
    p = ROLE_PERMISSIONS
    assert p[UserRole.VIEWER] == {Permission.INCIDENTS_READ}
    assert Permission.ACTIONS_APPROVE not in p[UserRole.RESPONDER]
    assert Permission.ACTIONS_APPROVE in p[UserRole.APPROVER]
    assert Permission.USERS_MANAGE not in p[UserRole.APPROVER]


# -------------------------------------------------------------- passwords


def test_password_hash_roundtrip_and_argon2id() -> None:
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password(h, "correct horse battery")
    assert not passwords.verify_password(h, "wrong horse battery")


def test_verify_without_hash_never_matches() -> None:
    assert not passwords.verify_password(None, "anything")
    assert not passwords.verify_password(None, "dummy-password-for-constant-time-checks")


def test_verify_rejects_garbage_and_huge_input() -> None:
    assert not passwords.verify_password("not-a-hash", "x")
    h = passwords.hash_password("correct horse battery")
    assert not passwords.verify_password(h, "x" * 5000)


@pytest.mark.parametrize(
    ("password", "ok"),
    [("short", False), ("a" * 12, True), (" leading-space-pw", False), ("x" * 2000, False)],
)
def test_password_policy(password: str, ok: bool) -> None:
    if ok:
        passwords.validate_password(password, min_length=12)
    else:
        with pytest.raises(passwords.PasswordPolicyError):
            passwords.validate_password(password, min_length=12)


# ----------------------------------------------------------------- tokens


def test_token_hash_is_keyed_and_domain_separated() -> None:
    t = tokens.new_token()
    h = tokens.hash_token(SECRET, t, purpose="session")
    assert len(h) == 64
    assert h != tokens.hash_token(SECRET, t, purpose="invite")
    assert h != tokens.hash_token(SECRET + "x", t, purpose="session")


def test_api_key_format() -> None:
    key = tokens.new_api_key()
    assert key.plaintext.startswith(f"sk_live_{key.prefix}_")
    assert tokens.parse_api_key(key.plaintext) == key.prefix
    assert tokens.parse_api_key("sk_live_short") is None
    assert tokens.parse_api_key("Bearer " + key.plaintext) is None


def test_csrf_token_bound_to_session() -> None:
    a = tokens.csrf_token_for(SECRET, "a" * 64)
    assert a == tokens.csrf_token_for(SECRET, "a" * 64)
    assert a != tokens.csrf_token_for(SECRET, "b" * 64)
    assert tokens.constant_time_equals(a, a)
    assert not tokens.constant_time_equals(a, a[:-1] + "0")


# ------------------------------------------------------------- rate limit


def test_rate_limiter_window() -> None:
    now = [0.0]
    rl = RateLimiter(limit=3, window_seconds=60, clock=lambda: now[0])
    assert [rl.hit("k") for _ in range(3)] == [None, None, None]
    retry = rl.hit("k")
    assert retry is not None
    assert 59 <= retry <= 60
    assert rl.hit("other") is None  # keys are independent
    now[0] = 61
    assert rl.hit("k") is None


# ------------------------------------------------------------------ audit


def test_redaction_is_recursive() -> None:
    out = audit.redact({"email": "a@b.c", "password": "p", "nested": [{"api_key": "k", "n": 1}]})
    assert out == {
        "email": "a@b.c",
        "password": audit.REDACTED,
        "nested": [{"api_key": audit.REDACTED, "n": 1}],
    }


def test_hash_changes_with_any_field() -> None:
    base = {
        "ts": datetime(2026, 11, 16, tzinfo=UTC),
        "actor_type": "user",
        "actor_id": "u1",
        "action": "user.update",
        "target_type": "user",
        "target_id": "u2",
        "details": {"role": "admin"},
        "ip": "203.0.113.1",
    }
    h = audit.compute_hash(audit.GENESIS_HASH, **base)  # type: ignore[arg-type]
    assert h == audit.compute_hash(audit.GENESIS_HASH, **base)  # type: ignore[arg-type]
    assert h != audit.compute_hash("1" * 64, **base)  # type: ignore[arg-type]
    for field, value in [("action", "user.delete"), ("details", {"role": "viewer"})]:
        assert h != audit.compute_hash(audit.GENESIS_HASH, **{**base, field: value})  # type: ignore[arg-type]


# --------------------------------------------------------------- settings


def test_prod_rejects_default_secret_and_insecure_cookies() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(env=Environment.PROD, _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings(
            env=Environment.PROD,
            secret_key="x" * 40,
            internal_auth_secret="y" * 40,
            cookie_secure=False,
            _env_file=None,
        )  # type: ignore[call-arg, arg-type]


# ---------------------------------------------------- route declarations


def test_every_route_declares_exactly_one_access_rule() -> None:
    """The guard from the roadmap: an endpoint without a declaration fails CI."""
    table = route_table(_app())
    assert table, "no routes found"
    offenders = [f"{r.method} {r.path}: {r.access}" for r in table if len(r.access) != 1]
    assert offenders == []


def test_route_walk_sees_every_openapi_operation() -> None:
    """Cross-check: if FastAPI internals change and hide routes, this fails."""
    app = _app()
    spec = {
        (method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops
    }
    # Internal routes are deliberately left out of the public API schema.
    walked = {(r.method, r.path) for r in route_table(app) if r.access[0].kind != "internal"}
    assert walked == spec


def test_undeclared_route_is_detected() -> None:
    app = _app()

    @app.get("/api/v1/oops")
    async def oops() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    bad = [r for r in route_table(app) if not r.access]
    assert [(r.method, r.path) for r in bad] == [("GET", "/api/v1/oops")]


def test_committed_rbac_doc_is_up_to_date() -> None:
    committed = Path(__file__).resolve().parents[3] / "docs" / "rbac.md"
    assert committed.read_text() == render(_app()), "run `make rbac-docs`"


# ---------------------------------------------------- anonymous requests


def test_anonymous_gets_401_problem_json() -> None:
    with TestClient(_app(), base_url="https://testserver") as client:
        r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["title"] == "Unauthorized"


def test_validation_errors_never_echo_input() -> None:
    with TestClient(_app(), base_url="https://testserver") as client:
        r = client.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "s3cr3t!"})
    assert r.status_code == 422
    assert "s3cr3t!" not in r.text
    assert r.json()["errors"]
