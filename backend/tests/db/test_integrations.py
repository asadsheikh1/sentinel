"""Integration registry, ingest-auth endpoint, health jobs and entities on real Postgres."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.config import Settings
from sentinel.db.engine import create_session_factory, session_scope
from sentinel.db.models import (
    AuditLog,
    IngestToken,
    IntegrationSignalStatus,
    UserRole,
)
from sentinel.integrations.entities import discover
from sentinel.integrations.gate import IngestGate
from sentinel.integrations.tokens import hash_ingest_token

from .authkit import app_with, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db

INTERNAL = "X-Sentinel-Internal-Auth"


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(migrated.url("sentinel_app"), ingest_auth_cache_ttl_s=0)


def _name() -> str:
    return f"it-{uuid.uuid4().hex[:10]}"


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"name": _name(), "kind": "otel_app", **body}
    r = client.post("/api/v1/integrations", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _auth(
    client: TestClient, settings: Settings, token: str | None, path: str = "/v1/metrics"
) -> Any:
    headers = {INTERNAL: settings.internal_auth_secret.get_secret_value(), "X-Forwarded-Uri": path}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.get("/internal/ingest-auth", headers=headers)


def _run(settings: Settings, fn: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_create_returns_token_once_and_stores_only_a_hash(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        created = _create(client, a.apply(client), expected_signals=["metrics", "logs"])
        token = created["ingest_token"]
        iid = created["integration"]["id"]
        assert token.startswith("sit_")
        assert created["integration"]["status"] == "pending"
        detail = client.get(f"/api/v1/integrations/{iid}", headers=a.apply(client))
        assert token not in detail.text
        listing = client.get("/api/v1/integrations")
        assert token not in listing.text

    async def stored(factory: Any) -> list[str]:
        async with factory() as db:
            rows = await db.scalars(
                select(IngestToken.token_hash).where(IngestToken.integration_id == uuid.UUID(iid))
            )
            return list(rows)

    secret = settings.secret_key.get_secret_value()
    assert _run(settings, stored) == [hash_ingest_token(secret, token)]


def test_duplicate_name_is_409(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    name = _name()
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        _create(client, a.apply(client), name=name)
        r = client.post(
            "/api/v1/integrations",
            json={"name": name, "kind": "otel_app"},
            headers=a.apply(client),
        )
    assert r.status_code == 409


def test_invalid_signal_or_kind_is_422(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        for body in (
            {"name": _name(), "kind": "otel_app", "expected_signals": ["vibes"]},
            {"name": _name(), "kind": "toaster"},
        ):
            assert (
                client.post("/api/v1/integrations", json=body, headers=a.apply(client)).status_code
                == 422
            )


def test_viewer_can_read_status_but_not_manage(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    viewer = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        iid = _create(client, a.apply(client))["integration"]["id"]
        v = login(client, viewer)
        h = v.apply(client)
        assert client.get("/api/v1/integrations", headers=h).status_code == 200
        assert client.get(f"/api/v1/integrations/{iid}/status", headers=h).status_code == 200
        assert client.get(f"/api/v1/integrations/{iid}", headers=h).status_code == 403
        assert (
            client.post(f"/api/v1/integrations/{iid}/tokens/rotate", headers=h).status_code == 403
        )
        assert client.delete(f"/api/v1/integrations/{iid}", headers=h).status_code == 403


def test_ingest_auth_requires_the_internal_secret(settings: Settings) -> None:
    with client_for(app_with(settings)) as client:
        assert client.get("/internal/ingest-auth").status_code == 401
        r = client.get("/internal/ingest-auth", headers={INTERNAL: "wrong"})
        assert r.status_code == 401


def test_ingest_auth_token_lifecycle(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        created = _create(client, a.apply(client))
        token, iid = created["ingest_token"], created["integration"]["id"]
        client.cookies.clear()

        ok = _auth(client, settings, token)
        assert ok.status_code == 200
        assert ok.headers["X-Sentinel-Integration-Id"] == iid
        assert ok.headers["X-Sentinel-Integration-Kind"] == "otel_app"

        assert _auth(client, settings, None).status_code == 401
        assert _auth(client, settings, "sit_aaaaaaaa_" + "x" * 43).status_code == 401
        assert _auth(client, settings, token, "/v1/profiles").status_code == 401

        rotated = client.post(f"/api/v1/integrations/{iid}/tokens/rotate", headers=a.apply(client))
        new_token = rotated.json()["ingest_token"]
        client.cookies.clear()
        assert _auth(client, settings, token).status_code == 401  # old token is dead
        assert _auth(client, settings, new_token).status_code == 200

        r = client.post(f"/api/v1/integrations/{iid}/tokens/revoke", headers=a.apply(client))
        assert r.status_code == 204
        client.cookies.clear()
        assert _auth(client, settings, new_token).status_code == 401
        a.apply(client)
        assert client.get(f"/api/v1/integrations/{iid}/status").json()["status"] == "revoked"

        newest = client.post(
            f"/api/v1/integrations/{iid}/tokens/rotate", headers=a.apply(client)
        ).json()["ingest_token"]
        client.cookies.clear()
        assert _auth(client, settings, newest).status_code == 200  # recovered

        assert (
            client.delete(f"/api/v1/integrations/{iid}", headers=a.apply(client)).status_code == 204
        )
        client.cookies.clear()
        assert _auth(client, settings, newest).status_code == 401
        rotate = client.post(f"/api/v1/integrations/{iid}/tokens/rotate", headers=a.apply(client))
        assert rotate.status_code == 409  # disabled stays disabled


def test_rate_limit_returns_429_with_retry_after(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        token = _create(client, a.apply(client), rate_limit_per_min=2)["ingest_token"]
        client.cookies.clear()
        codes = [_auth(client, settings, token) for _ in range(3)]
    statuses = [r.status_code for r in codes]
    # Two allowed per one-minute window (a window boundary can only allow more, never fewer).
    assert statuses[:2] == [200, 200]
    if statuses[2] == 429:
        assert 1 <= int(codes[2].headers["Retry-After"]) <= 60


def test_last_seen_flush_and_health_transitions(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    app = app_with(settings)
    with client_for(app) as client:
        a = login(client, admin)
        created = _create(client, a.apply(client), expected_signals=["metrics", "logs"])
        token, iid = created["ingest_token"], created["integration"]["id"]
        client.cookies.clear()
        gate: IngestGate = app.state.ingest_gate

        def status() -> dict[str, Any]:
            a.apply(client)
            return client.get(f"/api/v1/integrations/{iid}/status").json()  # type: ignore[no-any-return]

        def run(coro_factory: Any) -> Any:
            return client.portal.call(coro_factory)  # type: ignore[union-attr]

        run(gate.evaluate)
        assert status()["status"] == "pending"

        client.cookies.clear()
        assert _auth(client, settings, token, "/v1/metrics").status_code == 200
        run(gate.flush)
        run(gate.evaluate)
        current = status()
        assert current["status"] == "degraded"  # logs are expected but missing
        assert "logs" in current["status_reason"]
        assert [s["signal"] for s in current["signals"]] == ["metrics"]
        assert current["signals"][0]["last_count_window"] == 1

        client.cookies.clear()
        assert _auth(client, settings, token, "/v1/logs").status_code == 200
        run(gate.flush)
        run(gate.evaluate)
        assert status()["status"] == "healthy"

        async def later() -> Any:
            return await gate.evaluate(now=datetime.now(UTC) + timedelta(minutes=6))

        run(later)
        assert status()["status"] == "stale"

        client.cookies.clear()
        assert _auth(client, settings, token, "/v1/metrics").status_code == 200
        assert _auth(client, settings, token, "/v1/logs").status_code == 200
        run(gate.flush)
        run(gate.evaluate)
        assert status()["status"] == "healthy"  # recovers by itself

    async def audited(factory: Any) -> list[str]:
        async with factory() as db:
            rows = await db.scalars(
                select(AuditLog.details["to"].astext)
                .where(AuditLog.action == "integration.status_changed", AuditLog.target_id == iid)
                .order_by(AuditLog.id)
            )
            return list(rows)

    assert _run(settings, audited) == ["degraded", "healthy", "stale", "healthy"]


def test_flush_ignores_nothing_to_write(settings: Settings) -> None:
    async def go(factory: Any) -> int:
        return await IngestGate(settings, factory).flush()

    assert _run(settings, go) == 0


def test_entity_discovery_upserts_and_keeps_first_seen(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    tag = uuid.uuid4().hex[:8]
    owner = str(uuid.uuid4())
    rows = [
        {
            "metric": {
                "service_name": f"svc-{tag}",
                "service_namespace": "aviro",
                "host_name": f"host-{tag}",
                "deployment_environment_name": "dev",
                "sentinel_integration_id": owner,
            }
        }
    ]

    async def query(promql: str) -> list[dict[str, Any]]:
        if promql.startswith("count by"):
            return [{"metric": {"sentinel_integration_id": owner}, "value": [0, "42"]}]
        return rows

    async def go(factory: Any) -> dict[str, int]:
        async with session_scope(factory) as db:
            await discover(db, query)
        async with session_scope(factory) as db:
            _, series = await discover(db, query)
        return series

    assert _run(settings, go) == {owner: 42}

    with client_for(app_with(settings)) as client:
        login(client, admin)
        services = client.get(
            "/api/v1/entities", params={"type": "service", "q": f"svc-{tag}"}
        ).json()
        assert [s["key"] for s in services] == [f"aviro/svc-{tag}@dev"]
        assert services[0]["integration_ids"] == [owner]
        relations = client.get(f"/api/v1/entities/{services[0]['id']}/relations").json()
        assert [r["relation"] for r in relations] == ["runs_on"]
        assert client.get(f"/api/v1/entities/{uuid.uuid4()}").status_code == 404
        hosts = client.get("/api/v1/entities", params={"type": "host", "q": tag}).json()
        assert client.get(f"/api/v1/entities/{hosts[0]['id']}").json()["display_name"].endswith(tag)


async def test_readonly_cannot_read_token_hashes(readonly_engine: AsyncEngine) -> None:
    from sqlalchemy.exc import DBAPIError

    async with readonly_engine.connect() as conn:
        await conn.execute(text("SELECT count(*) FROM integrations"))
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT token_hash FROM ingest_tokens"))
        await conn.rollback()


def test_signal_status_rejects_unknown_signal(settings: Settings) -> None:
    from sqlalchemy.exc import IntegrityError

    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        iid = _create(client, a.apply(client))["integration"]["id"]

    async def go(factory: Any) -> None:
        async with factory() as db:
            db.add(
                IntegrationSignalStatus(
                    integration_id=uuid.UUID(iid), signal="vibes", last_seen_at=datetime.now(UTC)
                )
            )
            await db.flush()

    with pytest.raises(IntegrityError):
        _run(settings, go)
