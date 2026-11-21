"""Health checks API, prober identity and work list, probe state and events (real Postgres)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.checks.prober_identity import ensure_prober
from sentinel.checks.state import probe_state_pass
from sentinel.config import Settings
from sentinel.db.engine import create_session_factory
from sentinel.db.models import (
    AuditLog,
    Entity,
    Event,
    HealthCheck,
    IngestToken,
    Integration,
    IntegrationKind,
    ProbeState,
    UserRole,
)
from sentinel.prober.ssrf_guard import Guard

from .authkit import app_with, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db

PUBLIC = "93.184.215.14"
DNS = {
    "shop.example.com": [PUBLIC],
    "api.example.com": [PUBLIC],
    "internal.example.com": ["10.0.0.9"],
}


async def _resolve(host: str, port: int) -> list[str]:
    if host not in DNS:
        raise OSError("NXDOMAIN")
    return DNS[host]


@pytest.fixture
def settings(migrated: Pg, tmp_path: Path) -> Settings:
    return make_settings(
        migrated.url("sentinel_app"),
        ingest_auth_cache_ttl_s=0,
        health_check_min_interval_s=30,
        health_check_max=20,
        prober_token_file=str(tmp_path / "prober" / "token"),
    )


def _app(settings: Settings) -> Any:
    app = app_with(settings)
    app.state.check_guard = Guard(resolver=_resolve)
    return app


def _run(settings: Settings, fn: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _integration(client: TestClient, h: dict[str, str], kind: str = "http_check") -> str:
    r = client.post(
        "/api/v1/integrations",
        json={
            "name": f"checks-{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "expected_signals": ["metrics"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["integration"]["id"])


def _check(iid: str, **overrides: Any) -> dict[str, Any]:
    return {
        "integration_id": iid,
        "name": f"shop-{uuid.uuid4().hex[:6]}",
        "kind": "http",
        "target": "https://shop.example.com/health",
        "interval_s": 30,
        "timeout_s": 5,
        **overrides,
    }


# ------------------------------------------------------------------ CRUD


def test_create_list_update_delete_with_audit_and_entity(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(_app(settings)) as client:
        a = login(client, admin)
        iid = _integration(client, a.apply(client))
        body = _check(iid, assertions={"expected_status": [200], "body_contains": "ok"})
        r = client.post("/api/v1/checks", json=body, headers=a.apply(client))
        assert r.status_code == 201, r.text
        check = r.json()
        assert check["state"] == "unknown"
        assert check["target"] == "https://shop.example.com/health"
        assert check["entity_id"]
        cid = check["id"]

        assert [c["id"] for c in client.get("/api/v1/checks").json()].count(cid) == 1
        r = client.patch(f"/api/v1/checks/{cid}", json={"interval_s": 60}, headers=a.apply(client))
        assert r.status_code == 200, r.text
        assert r.json()["interval_s"] == 60
        assert r.json()["assertions"] == {"expected_status": [200], "body_contains": "ok"}
        r = client.patch(
            f"/api/v1/checks/{cid}",
            json={"kind": "tls", "target": "shop.example.com"},
            headers=a.apply(client),
        )
        assert r.status_code == 200, r.text
        assert (r.json()["target"], r.json()["assertions"]) == ("shop.example.com:443", {})
        assert client.delete(f"/api/v1/checks/{cid}", headers=a.apply(client)).status_code == 204
        assert client.get(f"/api/v1/checks/{cid}").status_code == 404

    async def check_db(factory: Any) -> None:
        async with factory() as db:
            actions = list(
                await db.scalars(
                    select(AuditLog.action).where(AuditLog.target_id == cid).order_by(AuditLog.id)
                )
            )
            assert actions == ["check.create", "check.update", "check.update", "check.delete"]
            entity = await db.scalar(
                select(Entity).where(Entity.id == uuid.UUID(check["entity_id"]))
            )
            assert entity is not None
            assert entity.type.value == "endpoint"
            assert entity.key == "https://shop.example.com/health"

    _run(settings, check_db)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"target": "http://169.254.169.254/latest/meta-data/"}, "Target not allowed"),
        ({"target": "http://localhost:8000/"}, "Target not allowed"),
        ({"target": "http://[::1]/"}, "Target not allowed"),
        ({"target": "http://2130706433/"}, "Target not allowed"),
        ({"target": "http://internal.example.com/"}, "Target not allowed"),
        ({"target": "http://nxdomain.example.com/"}, "does not resolve"),
        ({"target": "file:///etc/passwd"}, "Target not allowed"),
        ({"kind": "tcp", "target": "10.0.0.5:5432"}, "Target not allowed"),
        ({"kind": "tcp", "target": "shop.example.com"}, "needs a port"),
        ({"interval_s": 10, "timeout_s": 5}, "at least 30 seconds"),
        ({"timeout_s": 40}, "timeout_s"),
        ({"interval_s": 30, "timeout_s": 30}, "shorter than interval_s"),
        ({"assertions": {"unknown": 1}}, "assertions"),
        (
            {"kind": "tcp", "target": "shop.example.com:443", "assertions": {"body_contains": "x"}},
            "assertions",
        ),
        ({"assertions": {"expected_status": [99]}}, "assertions"),
        ({"assertions": {"json_field": {"path": "a b"}}}, "assertions"),
    ],
)  # fmt: skip
def test_unsafe_or_invalid_checks_are_rejected(
    settings: Settings, override: dict[str, Any], message: str
) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(_app(settings)) as client:
        a = login(client, admin)
        iid = _integration(client, a.apply(client))
        r = client.post("/api/v1/checks", json=_check(iid, **override), headers=a.apply(client))
        assert r.status_code == 422, r.text
        assert message in r.json()["detail"], r.json()


def test_checks_must_belong_to_an_enabled_http_check_integration(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(_app(settings)) as client:
        a = login(client, admin)
        host = _integration(client, a.apply(client), kind="linux_host")
        r = client.post("/api/v1/checks", json=_check(host), headers=a.apply(client))
        assert r.status_code == 422
        r = client.post("/api/v1/checks", json=_check(str(uuid.uuid4())), headers=a.apply(client))
        assert r.status_code == 422
        iid = _integration(client, a.apply(client))
        assert (
            client.delete(f"/api/v1/integrations/{iid}", headers=a.apply(client)).status_code == 204
        )
        r = client.post("/api/v1/checks", json=_check(iid), headers=a.apply(client))
        assert r.status_code == 422
        assert "disabled" in r.json()["detail"]


def test_number_of_checks_is_capped(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)

    async def existing(factory: Any) -> int:
        async with factory() as db:
            return len((await db.scalars(select(HealthCheck.id))).all())

    with client_for(_app(settings)) as client:
        a = login(client, admin)
        iid = _integration(client, a.apply(client))
        responses = [
            client.post("/api/v1/checks", json=_check(iid), headers=a.apply(client))
            for _ in range(settings.health_check_max + 1 - _run(settings, existing))
        ]
        assert responses[-1].status_code == 409
        assert {r.status_code for r in responses[:-1]} <= {201}
        for r in responses[:-1]:  # the database is shared with the other tests
            client.delete(f"/api/v1/checks/{r.json()['id']}", headers=a.apply(client))


def test_only_managers_see_or_change_checks(settings: Settings) -> None:
    viewer = seed_user(settings, role=UserRole.VIEWER)
    with client_for(_app(settings)) as client:
        v = login(client, viewer)
        assert client.get("/api/v1/checks").status_code == 403
        r = client.post("/api/v1/checks", json=_check(str(uuid.uuid4())), headers=v.apply(client))
        assert r.status_code == 403
        assert client.get("/api/v1/events").status_code == 200  # viewers read events


# ------------------------------------------------------------------ prober identity


def test_prober_is_provisioned_once_and_reads_only_its_work_list(settings: Settings) -> None:
    token_file = Path(str(settings.prober_token_file))
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(_app(settings)) as client:  # the app provisions at start-up
        token = token_file.read_text().strip()
        assert token.startswith("sit_")
        assert oct(token_file.stat().st_mode & 0o777) == "0o600"
        a = login(client, admin)
        iid = _integration(client, a.apply(client))
        enabled = client.post("/api/v1/checks", json=_check(iid), headers=a.apply(client)).json()
        disabled = client.post(
            "/api/v1/checks", json=_check(iid, enabled=False), headers=a.apply(client)
        ).json()
        other_token = client.post(
            "/api/v1/integrations",
            json={"name": f"other-{uuid.uuid4().hex[:6]}", "kind": "http_check"},
            headers=a.apply(client),
        ).json()["ingest_token"]
        client.cookies.clear()

        r = client.get("/api/v1/prober/checks", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        ids = {c["id"] for c in r.json()["checks"]}
        assert enabled["id"] in ids
        assert disabled["id"] not in ids
        assert r.json()["version"]
        # Any other ingest token (or none, or a user session) is refused.
        bad = client.get(
            "/api/v1/prober/checks", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert bad.status_code == 401
        assert client.get("/api/v1/prober/checks").status_code == 401
        login(client, admin)
        assert client.get("/api/v1/prober/checks").status_code == 401

    async def again(factory: Any) -> tuple[Any, Any]:
        first = await ensure_prober(factory, settings)
        kept = token_file.read_text().strip()  # noqa: ASYNC240 - a test's tiny file
        token_file.write_text("garbage\n")  # noqa: ASYNC240
        second = await ensure_prober(factory, settings)
        async with factory() as db:
            prober_count = len(
                (
                    await db.scalars(
                        select(Integration.id).where(Integration.kind == IntegrationKind.PROBER)
                    )
                ).all()
            )
            live = (
                await db.scalars(
                    select(IngestToken).where(
                        IngestToken.integration_id == first, IngestToken.revoked_at.is_(None)
                    )
                )
            ).all()
        return (first, second, kept, prober_count, len(live)), None

    (first, second, kept, prober_count, live), _ = _run(settings, again)
    assert first == second
    assert kept == token  # a valid token file is kept across restarts
    assert token_file.read_text().strip() != "garbage"  # an invalid one is replaced
    assert prober_count == 1
    assert live == 1  # the replaced token was revoked


# ------------------------------------------------------------------ probe state


def _matrix(check_id: str, samples: list[tuple[datetime, int]]) -> list[dict[str, Any]]:
    return [
        {
            "metric": {"__name__": "probe_success", "sentinel_check_id": check_id},
            "values": [[t.timestamp(), str(v)] for t, v in samples],
        }
    ]


class FakeGate:
    def __init__(self) -> None:
        self.seen: list[tuple[uuid.UUID, str]] = []

    def mark_seen(self, integration_id: uuid.UUID, signal: str, at: datetime) -> None:
        self.seen.append((integration_id, signal))


def test_probe_state_changes_become_events(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(_app(settings)) as client:
        a = login(client, admin)
        iid = _integration(client, a.apply(client))
        check = client.post("/api/v1/checks", json=_check(iid), headers=a.apply(client)).json()
    cid = check["id"]
    t0 = datetime.now(UTC) - timedelta(minutes=4)
    at = [t0 + timedelta(seconds=30 * i) for i in range(8)]
    gate = FakeGate()

    async def step(factory: Any, samples: list[tuple[datetime, int]], now: datetime) -> Any:
        async def query(_: str) -> list[dict[str, Any]]:
            return _matrix(cid, samples)

        return await probe_state_pass(factory, query, gate, now=now)  # type: ignore[arg-type]

    def run(samples: list[tuple[datetime, int]], now: datetime) -> Any:
        return _run(settings, lambda f: step(f, samples, now))

    assert run([], at[0]) == []  # no results yet: stays unknown
    up = [(at[0], 1), (at[1], 1)]
    assert run(up, at[1]) == [(check["name"], "up")]
    one_fail = [*up, (at[2], 0)]
    assert run(one_fail, at[2]) == []  # one failure is not "down"
    down = [*one_fail, (at[3], 0)]
    assert run(down, at[3]) == [(check["name"], "down")]
    assert run(down, at[3]) == []  # repeated pass: no new change
    recovered = [*down, (at[4], 1)]
    assert run(recovered, at[4]) == [(check["name"], "up")]
    # Results stop arriving: unknown, but that's not an event.
    assert run(recovered, at[4] + timedelta(minutes=10)) == [(check["name"], "unknown")]
    assert (uuid.UUID(iid), "metrics") in gate.seen

    async def stored(factory: Any) -> list[Event]:
        async with factory() as db:
            return list(
                await db.scalars(
                    select(Event)
                    .where(Event.payload["check_id"].astext == cid)
                    .order_by(Event.occurred_at)
                )
            )

    events = _run(settings, stored)
    assert [(e.payload["from"], e.payload["to"], e.severity) for e in events] == [
        ("unknown", "up", "info"),
        ("up", "down", "critical"),
        ("down", "up", "info"),
    ]
    assert events[1].occurred_at == at[3]
    assert str(events[1].source_integration_id) == iid
    assert [str(x) for x in events[1].entity_ids] == [check["entity_id"]]

    viewer = seed_user(settings, role=UserRole.VIEWER)
    with client_for(_app(settings)) as client:
        login(client, viewer)
        listed = client.get(
            "/api/v1/events", params={"type": "probe_state", "entity": check["entity_id"]}
        ).json()
        assert [e["payload"]["to"] for e in listed] == ["up", "down", "up"]
        assert (
            client.get("/api/v1/events", params={"since": datetime.now(UTC).isoformat()}).json()
            == []
        )

    async def state(factory: Any) -> ProbeState:
        async with factory() as db:
            hc = await db.get(HealthCheck, uuid.UUID(cid))
            assert hc is not None
            return hc.state

    assert _run(settings, state) is ProbeState.UNKNOWN
