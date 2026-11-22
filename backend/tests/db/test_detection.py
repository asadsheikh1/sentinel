"""Signals, deployments, GitHub events, telemetry gaps and their APIs (real Postgres)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.config import Settings
from sentinel.db.engine import create_session_factory
from sentinel.db.models import (
    ApiKey,
    CheckKind,
    Entity,
    EntityType,
    Event,
    EventType,
    HealthCheck,
    Integration,
    IntegrationKind,
    IntegrationSignalStatus,
    IntegrationStatus,
    ProbeState,
    Signal,
    SignalKind,
    UserRole,
)
from sentinel.detection.gaps import gap_pass
from sentinel.ingestion.alertmanager_key import KEY_NAME, ensure_alertmanager_key
from sentinel.integrations.gate import IngestGate
from sentinel.security.webhooks import sign

from .authkit import app_with, as_key, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"
SECRET = "test-github-webhook-secret-0123456789"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def settings(migrated: Pg, tmp_path: Path) -> Settings:
    return make_settings(
        migrated.url("sentinel_app"),
        github_webhook_secret=SECRET,
        alertmanager_key_file=str(tmp_path / "am" / "api-key"),
    )


def _run(settings: Settings, fn: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _key(client: TestClient, admin: str, scopes: list[str]) -> str:
    a = login(client, admin)
    r = client.post(
        "/api/v1/api-keys",
        json={"name": f"k-{uuid.uuid4().hex[:6]}", "scopes": scopes},
        headers=a.apply(client),
    )
    assert r.status_code == 201, r.text
    client.cookies.clear()
    return str(r.json()["secret"])


def _unique(payload: dict[str, Any]) -> dict[str, Any]:
    """Fresh fingerprints, so tests sharing the database don't see each other's alerts."""
    salt = uuid.uuid4().hex[:8]
    for alert in payload["alerts"]:
        alert["fingerprint"] = alert["fingerprint"][:8] + salt
    return payload


# ------------------------------------------------------------------ Alertmanager


def test_alertmanager_alerts_become_signals_idempotently(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    firing = _unique(fixture("alertmanager_firing.json"))
    resolved = fixture("alertmanager_resolved.json")
    resolved["alerts"][0]["fingerprint"] = firing["alerts"][0]["fingerprint"]
    with client_for(app_with(settings)) as client:
        key = _key(client, admin, ["alerts:write"])
        h = as_key(client, key)
        r = client.post("/api/v1/ingest/alertmanager", json=firing, headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"created": 2, "updated": 0, "unchanged": 0}
        # Alertmanager retries and repeats: nothing new.
        again = client.post("/api/v1/ingest/alertmanager", json=firing, headers=h)
        assert again.json() == {"created": 0, "updated": 0, "unchanged": 2}
        # Resolution updates the same signal.
        done = client.post("/api/v1/ingest/alertmanager", json=resolved, headers=h)
        assert done.json() == {"created": 0, "updated": 1, "unchanged": 0}
        # A late "firing" delivery never re-opens a resolved episode.
        late = {**firing, "alerts": [firing["alerts"][0]]}
        assert client.post("/api/v1/ingest/alertmanager", json=late, headers=h).json() == {
            "created": 0,
            "updated": 0,
            "unchanged": 1,
        }

    async def stored(factory: Any) -> list[Signal]:
        async with factory() as db:
            fps = [a["fingerprint"] for a in firing["alerts"]]
            return list(
                await db.scalars(
                    select(Signal).where(Signal.fingerprint.in_(fps)).order_by(Signal.starts_at)
                )
            )

    signals = _run(settings, stored)
    assert [(s.kind, s.status.value, s.severity) for s in signals] == [
        (SignalKind.PROM_ALERT, "resolved", "critical"),
        (SignalKind.PROM_ALERT, "firing", "critical"),
    ]
    assert signals[0].title == "orders: 20% of requests fail"
    assert signals[0].service_name == "orders"
    assert signals[0].ends_at == datetime(2026, 11, 22, 9, 12, tzinfo=UTC)
    assert signals[1].ends_at is None  # still firing: Alertmanager's future endsAt ignored
    # The fixture names an integration that doesn't exist (e.g. deleted): not linked,
    # and the rest of the delivery is stored anyway.
    assert signals[1].source_integration_id is None
    assert signals[1].labels["sentinel_integration_id"] == "01a20000-0000-7000-8000-000000000001"


def test_alertmanager_needs_an_alerts_key(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    payload = _unique(fixture("alertmanager_firing.json"))
    with client_for(app_with(settings)) as client:
        wrong = _key(client, admin, ["deployments:write"])
        assert client.post("/api/v1/ingest/alertmanager", json=payload).status_code == 401
        r = client.post("/api/v1/ingest/alertmanager", json=payload, headers=as_key(client, wrong))
        assert r.status_code == 403
        bad = {**payload, "version": "3"}
        good = _key(client, admin, ["alerts:write"])
        r = client.post("/api/v1/ingest/alertmanager", json=bad, headers=as_key(client, good))
        assert r.status_code == 422


def test_signals_are_attributed_to_inventory_entities(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    salt = uuid.uuid4().hex[:6]

    async def seed(factory: Any) -> dict[str, uuid.UUID]:
        async with factory() as db, db.begin():
            ids = {}
            for kind, key in [
                (EntityType.SERVICE, f"aviro/orders-{salt}@dev"),
                (EntityType.HOST, f"host-{salt}"),
                (EntityType.CONTAINER, f"host-{salt}/aviro_orders_{salt}"),
            ]:
                attrs = (  # as entity discovery records them
                    {"service.name": f"orders-{salt}", "service.namespace": "aviro",
                     "deployment.environment.name": "dev"}
                    if kind is EntityType.SERVICE else {}
                )  # fmt: skip
                e = Entity(type=kind, key=key, display_name=key.split("/")[-1], attributes=attrs)
                db.add(e)
                await db.flush()
                ids[kind.value] = e.id
            return ids

    ids = _run(settings, seed)
    alerts = [
        (
            {"alertname": "A", "job": f"aviro/orders-{salt}", "service_name": f"orders-{salt}"},
            "service",
        ),
        ({"alertname": "B", "host_name": f"host-{salt}"}, "host"),
        (
            {
                "alertname": "C",
                "host_name": f"host-{salt}",
                "container_name": f"aviro_orders_{salt}",
            },
            "container",
        ),
    ]
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": labels,
                "startsAt": "2026-11-22T10:00:00Z",
                "fingerprint": uuid.uuid4().hex[:16],
            }
            for labels, _ in alerts
        ],
    }
    with client_for(app_with(settings)) as client:
        key = _key(client, admin, ["alerts:write"])
        client.post("/api/v1/ingest/alertmanager", json=payload, headers=as_key(client, key))
        viewer = seed_user(settings, role=UserRole.VIEWER)
        login(client, viewer)
        for (labels, kind), sent in zip(alerts, payload["alerts"], strict=True):
            found = [
                s
                for s in client.get("/api/v1/signals", params={"entity": str(ids[kind])}).json()
                if s["labels"]["alertname"] == labels["alertname"]
            ]
            assert len(found) == 1, (kind, sent)
        firing = client.get("/api/v1/signals", params={"status": "firing", "kind": "prom_alert"})
        assert firing.status_code == 200

        # A deployment names only the service ("orders-…"); the inventory knows it
        # as "aviro/orders-…@dev". It must still be linked (found in the S01 run).
        deploy_key = _key(client, admin, ["deployments:write"])
        r = client.post(
            "/api/v1/ingest/deployments",
            json={"service": f"orders-{salt}", "version": "v9"},
            headers=as_key(client, deploy_key),
        )
        assert r.status_code == 201, r.text
        assert r.json()["entity_id"] == str(ids["service"])


# ------------------------------------------------------------------ deployments


def test_deployments_are_recorded_once_with_the_previous_commit(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    service = f"orders-{uuid.uuid4().hex[:6]}"
    first = {
        "service": service,
        "version": "v1",
        "commit_sha": "a" * 40,
        "deployed_at": "2026-11-22T08:00:00Z",
        "deployed_by": "dev",
    }
    second = {
        "service": service,
        "version": "v2",
        "commit_sha": "b" * 40,
        "deployed_at": "2026-11-22T09:00:00Z",
        "source": "script",
    }
    with client_for(app_with(settings)) as client:
        key = _key(client, admin, ["deployments:write"])
        h = {**as_key(client, key), "Idempotency-Key": f"deploy-{service}-1"}
        r = client.post("/api/v1/ingest/deployments", json=first, headers=h)
        assert r.status_code == 201, r.text
        assert client.post("/api/v1/ingest/deployments", json=first, headers=h).status_code == 200
        r = client.post("/api/v1/ingest/deployments", json=second, headers=as_key(client, key))
        assert r.status_code == 201
        assert r.json()["previous_commit_sha"] == "a" * 40
        # Without an Idempotency-Key, an identical body is still one deployment.
        r = client.post("/api/v1/ingest/deployments", json=second, headers=as_key(client, key))
        assert r.status_code == 200
        bad = {**first, "commit_sha": "zzz"}
        assert (
            client.post(
                "/api/v1/ingest/deployments", json=bad, headers=as_key(client, key)
            ).status_code
            == 422
        )
        h_bad = {**as_key(client, key), "Idempotency-Key": "has spaces"}
        assert (
            client.post("/api/v1/ingest/deployments", json=first, headers=h_bad).status_code == 422
        )
        wrong = _key(client, admin, ["alerts:write"])
        assert (
            client.post(
                "/api/v1/ingest/deployments", json=first, headers=as_key(client, wrong)
            ).status_code
            == 403
        )

        viewer = seed_user(settings, role=UserRole.VIEWER)
        login(client, viewer)
        listed = client.get("/api/v1/deployments", params={"service": service}).json()
        assert [(d["version"], d["source"]) for d in listed] == [("v2", "script"), ("v1", "ci")]
        events = client.get("/api/v1/events", params={"type": "deployment"}).json()
        assert sum(1 for e in events if e["payload"]["service"] == service) == 2


# ------------------------------------------------------------------ GitHub


def _github(
    client: TestClient,
    event: str,
    body: dict[str, Any],
    *,
    secret: str = SECRET,
    delivery: str | None = None,
) -> Any:
    raw = json.dumps(body).encode()
    return client.post(
        "/api/v1/ingest/github",
        content=raw,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "X-Hub-Signature-256": sign(secret, raw),
            "Content-Type": "application/json",
        },
    )


def test_github_signed_events_are_accepted_and_forged_ones_rejected(settings: Settings) -> None:
    with client_for(app_with(settings)) as client:
        delivery = str(uuid.uuid4())
        r = _github(client, "push", fixture("github_push.json"), delivery=delivery)
        assert r.status_code == 202, r.text
        assert r.json() == {"event": "push", "result": "created"}
        again = _github(client, "push", fixture("github_push.json"), delivery=delivery)
        assert again.json()["result"] == "duplicate"
        # Forged: wrong secret, tampered body, no signature.
        assert (
            _github(client, "push", fixture("github_push.json"), secret="guess").status_code == 401
        )
        raw = json.dumps(fixture("github_push.json")).encode()
        tampered = client.post(
            "/api/v1/ingest/github",
            content=raw + b" ",
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "d1",
                "X-Hub-Signature-256": sign(SECRET, raw),
            },
        )
        assert tampered.status_code == 401
        unsigned = client.post(
            "/api/v1/ingest/github",
            content=raw,
            headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d2"},
        )
        assert unsigned.status_code == 401
        assert _github(client, "ping", {"zen": "hi"}).json()["result"] == "pong"
        assert _github(client, "issues", {"action": "opened"}).json()["result"] == "ignored"
        assert _github(client, "push", {"no": "repository"}).status_code == 400

    async def pushed(factory: Any) -> Event:
        async with factory() as db:
            event = await db.scalar(
                select(Event).where(Event.dedupe_key == f"github:delivery:{delivery}")
            )
            assert event is not None
            return event

    event = _run(settings, pushed)
    assert event.type is EventType.PUSH
    assert event.payload["commit_count"] == 2
    messages = [c["message"] for c in event.payload["commits"]]
    assert messages[0] == "orders: support gift messages on checkout"  # first line only
    assert "ghp_" not in json.dumps(event.payload)  # secrets in commit messages are redacted


def test_github_deployments_and_workflow_runs(settings: Settings) -> None:
    body = fixture("github_deployment_status.json")
    body["deployment"]["id"] = int(uuid.uuid4().int % 10**9)
    service = f"orders-{uuid.uuid4().hex[:6]}"
    body["deployment"]["payload"]["service"] = service
    with client_for(app_with(settings)) as client:
        in_progress = json.loads(json.dumps(body))
        in_progress["deployment_status"]["state"] = "in_progress"
        assert _github(client, "deployment_status", in_progress).json()["result"] == "created"
        assert _github(client, "deployment_status", body).json()["result"] == "updated"
        run = _github(client, "workflow_run", fixture("github_workflow_run.json"))
        assert run.json()["result"] == "created"
        queued = {**fixture("github_workflow_run.json"), "action": "requested"}
        assert _github(client, "workflow_run", queued).json()["result"] == "ignored"
        viewer = seed_user(settings, role=UserRole.VIEWER)
        login(client, viewer)
        deployments = client.get("/api/v1/deployments", params={"service": service}).json()
        assert [(d["status"], d["source"], d["environment"]) for d in deployments] == [
            ("succeeded", "github", "production")
        ]
        runs = client.get("/api/v1/events", params={"type": "workflow_run"}).json()
        assert any(
            e["payload"]["conclusion"] == "failure" and e["severity"] == "warning" for e in runs
        )


def test_github_webhook_is_off_without_a_secret(migrated: Pg) -> None:
    settings = make_settings(migrated.url("sentinel_app"))
    with client_for(app_with(settings)) as client:
        r = _github(client, "ping", {"zen": "hi"})
        assert r.status_code == 401
        assert r.json()["detail"] == "The GitHub webhook is not configured."


# ------------------------------------------------------------------ telemetry gaps


def test_stale_integrations_get_one_classified_gap_that_resolves(settings: Settings) -> None:
    """S15 (agent stopped, probe OK) and S16 (host down, probe failing)."""
    admin = seed_user(settings, role=UserRole.ADMIN)
    salt = uuid.uuid4().hex[:6]
    host = f"web-{salt}"
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        r = client.post(
            "/api/v1/integrations",
            json={"name": f"agent-{salt}", "kind": "linux_host", "expected_signals": ["metrics"]},
            headers=a.apply(client),
        )
        iid = uuid.UUID(r.json()["integration"]["id"])
        r = client.post(
            "/api/v1/integrations",
            json={"name": f"checks-{salt}", "kind": "http_check"},
            headers=a.apply(client),
        )
        checks_iid = uuid.UUID(r.json()["integration"]["id"])

    async def setup(factory: Any) -> uuid.UUID:
        async with factory() as db, db.begin():
            db.add(Entity(type=EntityType.HOST, key=host, display_name=host, integration_ids=[iid]))
            check = HealthCheck(
                integration_id=checks_iid,
                name="web",
                kind=CheckKind.HTTP,
                target=f"https://{host}.example.com/health",
                interval_s=30,
                timeout_s=5,
                state=ProbeState.UP,
            )
            db.add(check)
            db.add(
                IntegrationSignalStatus(
                    integration_id=iid,
                    signal="metrics",
                    last_seen_at=datetime.now(UTC) - timedelta(minutes=10),
                )
            )
            await db.execute(
                update(Integration)
                .where(Integration.id == iid)
                .values(status=IntegrationStatus.STALE)
            )
            await db.flush()
            return check.id

    check_id = _run(settings, setup)

    async def gaps(factory: Any) -> list[Signal]:
        async with factory() as db:
            return list(
                await db.scalars(
                    select(Signal).where(
                        Signal.kind == SignalKind.TELEMETRY_GAP, Signal.source_integration_id == iid
                    )
                )
            )

    async def set_probe(factory: Any, state: ProbeState) -> None:
        async with factory() as db, db.begin():
            await db.execute(
                update(HealthCheck).where(HealthCheck.id == check_id).values(state=state)
            )

    async def set_status(factory: Any, status: IntegrationStatus) -> None:
        async with factory() as db, db.begin():
            await db.execute(update(Integration).where(Integration.id == iid).values(status=status))

    # S15: agent silent, its host's endpoint check passes -> agent problem, info.
    assert (f"agent-{salt}", "opened:agent_problem") in _run(settings, gap_pass)
    [gap] = _run(settings, gaps)
    assert (gap.status.value, gap.severity, gap.payload["classification"]) == (
        "firing",
        "info",
        "agent_problem",
    )
    assert _run(settings, gap_pass) == [] or all(
        n != f"agent-{salt}" for n, _ in _run(settings, gap_pass)
    )
    # S16: the check fails too -> host or network down, critical (same signal, re-classified).
    _run(settings, lambda f: set_probe(f, ProbeState.DOWN))
    assert (f"agent-{salt}", "reclassified:host_or_network_down") in _run(settings, gap_pass)
    [gap] = _run(settings, gaps)
    assert (gap.severity, gap.payload["classification"]) == ("critical", "host_or_network_down")
    # Data flows again -> resolved.
    _run(settings, lambda f: set_status(f, IntegrationStatus.HEALTHY))
    assert (f"agent-{salt}", "resolved") in _run(settings, gap_pass)
    [gap] = _run(settings, gaps)
    assert gap.status.value == "resolved"
    assert gap.ends_at is not None
    # Flapping (stale again within 10 minutes): the same signal re-opens, no new one.
    _run(settings, lambda f: set_status(f, IntegrationStatus.STALE))
    assert (f"agent-{salt}", "reopened:host_or_network_down") in _run(settings, gap_pass)
    [gap] = _run(settings, gaps)
    assert (gap.status.value, gap.payload["reopened"]) == ("firing", 1)


def test_integration_status_changes_are_events(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        r = client.post(
            "/api/v1/integrations",
            json={
                "name": f"app-{uuid.uuid4().hex[:6]}",
                "kind": "otel_app",
                "expected_signals": ["metrics"],
            },
            headers=a.apply(client),
        )
        iid = uuid.UUID(r.json()["integration"]["id"])

    async def go(factory: Any) -> list[Event]:
        gate = IngestGate(settings, factory)
        async with factory() as db, db.begin():
            db.add(
                IntegrationSignalStatus(
                    integration_id=iid, signal="metrics", last_seen_at=datetime.now(UTC)
                )
            )
        await gate.evaluate()
        async with factory() as db:
            return list(
                await db.scalars(
                    select(Event).where(
                        Event.type == EventType.INTEGRATION_STATUS,
                        Event.source_integration_id == iid,
                    )
                )
            )

    events = _run(settings, go)
    assert [(e.payload["from"], e.payload["to"]) for e in events] == [("pending", "healthy")]


# ------------------------------------------------------------------ Alertmanager key


def test_alertmanager_key_is_provisioned_kept_and_replaced(settings: Settings) -> None:
    path = Path(str(settings.alertmanager_key_file))

    async def go(factory: Any) -> tuple[bool, bool, bool, int, list[str]]:
        first = await ensure_alertmanager_key(factory, settings)
        key = path.read_text().strip()  # noqa: ASYNC240
        kept = await ensure_alertmanager_key(factory, settings)
        path.write_text("garbage\n")  # noqa: ASYNC240 - a test's tiny file
        replaced = await ensure_alertmanager_key(factory, settings)
        async with factory() as db:
            live = (
                await db.scalars(
                    select(ApiKey).where(ApiKey.name == KEY_NAME, ApiKey.revoked_at.is_(None))
                )
            ).all()
        return first, kept, replaced, len(live), [key, *(k.scopes[0] for k in live)]

    first, kept, replaced, live, (key, *scopes) = _run(settings, go)
    assert (first, kept, replaced) == (True, False, True)
    assert live == 1
    assert scopes == ["alerts:write"]
    assert key.startswith("sk_live_")
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    with client_for(app_with(settings)) as client:
        payload = _unique(fixture("alertmanager_firing.json"))
        r = client.post(
            "/api/v1/ingest/alertmanager",
            json=payload,
            headers=as_key(client, path.read_text().strip()),
        )
        assert r.status_code == 200
        assert IntegrationKind  # imported for readers: the key is not tied to an integration
