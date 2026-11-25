"""Correlation into incidents, severity, lifecycle, timeline and the API (real Postgres).

Each test runs at its own far-future "now", so the correlator (which takes every
unattached signal of the last hours) sees only that test's signals.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.config import Settings
from sentinel.db.engine import create_session_factory
from sentinel.db.models import (
    Entity,
    EntityRelation,
    EntityType,
    Incident,
    IncidentEvent,
    IncidentSignal,
    IncidentStatus,
    Job,
    Severity,
    Signal,
    SignalKind,
    SignalStatus,
    UserRole,
)
from sentinel.detection.correlator import LOCK_KEY, CorrelationResult, correlation_pass
from sentinel.ingestion.deployments import DeploymentIn, record_deployment

from .authkit import app_with, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(migrated.url("sentinel_app"))


@pytest.fixture
def t0() -> datetime:
    """A "now" no other test uses."""
    return datetime(2031, 1, 1, tzinfo=UTC) + timedelta(days=uuid.uuid4().int % 100_000)


def _run(settings: Settings, fn: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _db(settings: Settings, fn: Any) -> Any:
    async def go(factory: Any) -> Any:
        async with factory() as db, db.begin():
            return await fn(db)

    return _run(settings, go)


def correlate(settings: Settings, now: datetime) -> CorrelationResult:
    result: CorrelationResult = _run(settings, lambda f: correlation_pass(f, settings, now=now))
    return result


# ------------------------------------------------------------------ fixtures in the db


def entity(settings: Settings, kind: EntityType, name: str, **attrs: str) -> uuid.UUID:
    async def go(db: Any) -> uuid.UUID:
        e = Entity(type=kind, key=f"{name}-{uuid.uuid4().hex[:6]}", display_name=name,
                   attributes=attrs, criticality=attrs.pop("criticality", None))  # fmt: skip
        db.add(e)
        await db.flush()
        return e.id

    result: uuid.UUID = _db(settings, go)
    return result


def relate(settings: Settings, src: uuid.UUID, dst: uuid.UUID, relation: str = "runs_on") -> None:
    async def go(db: Any) -> None:
        db.add(EntityRelation(from_entity_id=src, to_entity_id=dst, relation=relation))

    _db(settings, go)


def signal(
    settings: Settings,
    *,
    at: datetime,
    entity_id: uuid.UUID | None,
    service: str | None,
    title: str,
    severity: str = "critical",
    kind: SignalKind = SignalKind.PROM_ALERT,
    status: SignalStatus = SignalStatus.FIRING,
    alertname: str = "ServiceHighErrorRatio",
    value: float | None = None,
) -> uuid.UUID:
    async def go(db: Any) -> uuid.UUID:
        s = Signal(
            kind=kind, status=status, severity=severity, title=title, entity_id=entity_id,
            service_name=service, fingerprint=uuid.uuid4().hex,
            labels={"alertname": alertname},
            payload={"annotations": {"value": str(value)}} if value is not None else {},
            starts_at=at, received_at=at, dedupe_key=f"test:{uuid.uuid4()}",
            ends_at=at if status is SignalStatus.RESOLVED else None,
        )  # fmt: skip
        db.add(s)
        await db.flush()
        return s.id

    result: uuid.UUID = _db(settings, go)
    return result


def resolve(settings: Settings, signal_id: uuid.UUID, at: datetime) -> None:
    async def go(db: Any) -> None:
        s = await db.get(Signal, signal_id)
        s.status, s.ends_at = SignalStatus.RESOLVED, at

    _db(settings, go)


def deploy(settings: Settings, service: str, at: datetime, sha: str) -> uuid.UUID:
    async def go(db: Any) -> uuid.UUID:
        d, _ = await record_deployment(
            db, DeploymentIn(service=service, version="v2", commit_sha=sha, deployed_at=at)
        )
        return d.id  # type: ignore[no-any-return]

    result: uuid.UUID = _db(settings, go)
    return result


def incident_of(settings: Settings, signal_id: uuid.UUID) -> Incident | None:
    async def go(db: Any) -> Incident | None:
        found: Incident | None = await db.scalar(
            select(Incident)
            .join(IncidentSignal, IncidentSignal.incident_id == Incident.id)
            .where(IncidentSignal.signal_id == signal_id)
        )
        return found

    result: Incident | None = _db(settings, go)
    return result


def reasons(settings: Settings, incident_id: uuid.UUID) -> dict[uuid.UUID, str]:
    async def go(db: Any) -> dict[uuid.UUID, str]:
        rows = await db.execute(
            select(IncidentSignal.signal_id, IncidentSignal.reason).where(
                IncidentSignal.incident_id == incident_id
            )
        )
        return dict(rows.all())

    result: dict[uuid.UUID, str] = _db(settings, go)
    return result


def events(settings: Settings, incident_id: uuid.UUID) -> list[IncidentEvent]:
    async def go(db: Any) -> list[IncidentEvent]:
        rows = await db.scalars(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.ts, IncidentEvent.id)
        )
        return list(rows.all())

    result: list[IncidentEvent] = _db(settings, go)
    return result


# ------------------------------------------------------------------ correlation tables


def test_s01_is_one_incident_with_its_deployment_and_escalates(
    settings: Settings, t0: datetime
) -> None:
    sha = uuid.uuid4().hex[:12]
    orders = entity(settings, EntityType.SERVICE, "orders", **{"service.name": "orders"})
    gateway = entity(settings, EntityType.SERVICE, "gateway", **{"service.name": "gateway"})
    deployment = deploy(settings, "orders", t0 - timedelta(minutes=2), sha)
    deploy(settings, "gateway", t0 - timedelta(minutes=2), sha)  # the same release
    # The new error group comes first (≈30 s), the alerts a minute later.
    eg = signal(settings, at=t0, entity_id=orders, service="orders", severity="warning",
                kind=SignalKind.ERROR_GROUP_NEW, title="orders: new error KeyError")  # fmt: skip
    first = correlate(settings, t0 + timedelta(seconds=10))
    assert len(first.opened) == 1

    alert_orders = signal(settings, at=t0 + timedelta(seconds=70), entity_id=orders,
                          service="orders", title="orders: 100% fail", value=1.0)  # fmt: skip
    alert_gateway = signal(settings, at=t0 + timedelta(seconds=75), entity_id=gateway,
                           service="gateway", title="gateway: 10% fail", value=0.1)  # fmt: skip
    second = correlate(settings, t0 + timedelta(seconds=90))
    assert second.opened == []

    incident = incident_of(settings, eg)
    assert incident is not None
    assert incident_of(settings, alert_orders).id == incident.id  # type: ignore[union-attr]
    assert incident_of(settings, alert_gateway).id == incident.id  # type: ignore[union-attr]
    assert reasons(settings, incident.id) == {
        eg: "first_signal",
        alert_orders: "same_entity",
        alert_gateway: "deploy_window",
    }
    assert incident.suspected_deployment_id == deployment
    assert incident.severity is Severity.SEV1  # every order failing (R2), raised from SEV3
    assert incident.status is IncidentStatus.OPEN

    types = [e.type for e in events(settings, incident.id)]
    assert types[:4] == ["opened", "signal_attached", "deployment", "investigation_queued"]
    assert types.count("signal_attached") == 3
    changes = [e.data for e in events(settings, incident.id) if e.type == "severity_changed"]
    assert changes[0]["from"] == "SEV3"
    assert changes[-1]["to"] == "SEV1"

    jobs = _db(settings, lambda db: db.scalar(select(func.count()).select_from(Job).where(
        Job.payload["incident_id"].astext == str(incident.id))))  # fmt: skip
    assert jobs == 1


def test_host_disk_full_and_errors_of_a_service_on_it_are_one_incident(
    settings: Settings, t0: datetime
) -> None:
    host = entity(settings, EntityType.HOST, "aviro-host")
    container = entity(settings, EntityType.CONTAINER, "aviro_orders_1")
    orders = entity(settings, EntityType.SERVICE, "orders")
    relate(settings, orders, container)
    relate(settings, container, host)
    other_host = entity(settings, EntityType.HOST, "unrelated-host")

    disk = signal(settings, at=t0, entity_id=host, service=None, alertname="HostFilesystemFull",
                  title="aviro-host: / is 96% full")  # fmt: skip
    errors = signal(settings, at=t0 + timedelta(minutes=2), entity_id=orders, service="orders",
                    title="orders: 40% fail", value=0.4)  # fmt: skip
    elsewhere = signal(settings, at=t0 + timedelta(minutes=3), entity_id=other_host, service=None,
                       alertname="HostFilesystemFull", title="unrelated-host: / is 95% full")  # fmt: skip  # noqa: E501
    result = correlate(settings, t0 + timedelta(minutes=4))
    assert len(result.opened) == 2

    incident = incident_of(settings, disk)
    assert incident is not None
    assert incident_of(settings, errors).id == incident.id  # type: ignore[union-attr]
    assert reasons(settings, incident.id)[errors] == "related_entity"
    assert incident_of(settings, elsewhere).id != incident.id  # type: ignore[union-attr]

    with client_for(app_with(settings)) as client:
        login(client, seed_user(settings, role=UserRole.VIEWER))
        detail = client.get(f"/api/v1/incidents/{incident.id}").json()
        assert {(e["id"], e["role"]) for e in detail["entities"]} == {
            (str(host), "origin"), (str(orders), "affected")
        }  # fmt: skip


def test_two_unrelated_faults_are_two_incidents(settings: Settings, t0: datetime) -> None:
    a = entity(settings, EntityType.SERVICE, "billing")
    b = entity(settings, EntityType.SERVICE, "search")
    host = entity(settings, EntityType.HOST, "shared-host")
    relate(settings, a, host)
    relate(settings, b, host)  # only share a host: not related
    s1 = signal(settings, at=t0, entity_id=a, service="billing", title="billing fails")
    s2 = signal(settings, at=t0 + timedelta(seconds=30), entity_id=b, service="search",
                title="search fails")  # fmt: skip
    assert len(correlate(settings, t0 + timedelta(minutes=1)).opened) == 2
    assert incident_of(settings, s1).id != incident_of(settings, s2).id  # type: ignore[union-attr]


def test_info_and_already_resolved_signals_never_open_an_incident(
    settings: Settings, t0: datetime
) -> None:
    host = entity(settings, EntityType.HOST, "agent-host")
    info = signal(settings, at=t0, entity_id=host, service=None, severity="info",
                  kind=SignalKind.TELEMETRY_GAP, title="agent problem")  # fmt: skip
    gone = signal(settings, at=t0, entity_id=None, service="flaky", title="blip",
                  status=SignalStatus.RESOLVED)  # fmt: skip
    result = correlate(settings, t0 + timedelta(minutes=1))
    assert result.opened == []
    assert incident_of(settings, info) is None
    assert incident_of(settings, gone) is None

    # But an info signal joins an incident about the same thing.
    signal(settings, at=t0 + timedelta(minutes=2), entity_id=host, service=None,
           alertname="HostFilesystemFull", title="disk full")  # fmt: skip
    correlate(settings, t0 + timedelta(minutes=3))
    joined = incident_of(settings, info)
    assert joined is not None


def test_a_pass_twice_changes_nothing_and_a_second_correlator_skips(
    settings: Settings, t0: datetime
) -> None:
    svc = entity(settings, EntityType.SERVICE, "cart")
    signal(settings, at=t0, entity_id=svc, service="cart", title="cart fails")
    assert len(correlate(settings, t0 + timedelta(seconds=5)).opened) == 1
    again = correlate(settings, t0 + timedelta(seconds=10))
    assert again.opened == [] and again.attached == []  # noqa: PT018

    async def locked(factory: Any) -> CorrelationResult:
        async with factory() as other, other.begin():
            await other.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))
            return await correlation_pass(factory, settings, now=t0)

    assert _run(settings, locked).skipped == "another correlator holds the lock"


def test_quiet_incidents_become_mitigated_and_a_new_firing_signal_reopens(
    settings: Settings, t0: datetime
) -> None:
    svc = entity(settings, EntityType.SERVICE, "stock")
    s1 = signal(settings, at=t0, entity_id=svc, service="stock", title="stock fails")
    correlate(settings, t0 + timedelta(seconds=5))
    resolve(settings, s1, t0 + timedelta(minutes=3))
    early = correlate(settings, t0 + timedelta(minutes=4))  # quiet for 1 minute only
    assert early.mitigated == []
    result = correlate(settings, t0 + timedelta(minutes=9))  # quiet for 6 (> 5) minutes
    incident = incident_of(settings, s1)
    assert incident is not None
    assert result.mitigated == [incident.ref]
    assert incident.status is IncidentStatus.MITIGATED
    assert incident.mitigated_at is not None

    s2 = signal(settings, at=t0 + timedelta(minutes=12), entity_id=svc, service="stock",
                title="stock fails again")  # fmt: skip
    correlate(settings, t0 + timedelta(minutes=13))
    again = incident_of(settings, s2)
    assert again is not None and again.id == incident.id  # noqa: PT018
    assert again.status is IncidentStatus.OPEN
    statuses = [(e.data["from"], e.data["to"], e.actor) for e in events(settings, incident.id)
                if e.type == "status_changed"]  # fmt: skip
    assert statuses == [("open", "mitigated", "system"), ("mitigated", "open", "system")]


def test_the_timeline_is_append_only(settings: Settings, t0: datetime) -> None:
    svc = entity(settings, EntityType.SERVICE, "ledger")
    s = signal(settings, at=t0, entity_id=svc, service="ledger", title="ledger fails")
    correlate(settings, t0 + timedelta(seconds=5))
    incident = incident_of(settings, s)
    assert incident is not None

    async def tamper(db: Any) -> None:
        await db.execute(
            text("UPDATE incident_events SET actor = 'mallory' WHERE incident_id = :i"),
            {"i": incident.id},
        )

    with pytest.raises(DBAPIError):
        _db(settings, tamper)

    async def delete(db: Any) -> None:
        await db.execute(
            text("DELETE FROM incident_events WHERE incident_id = :i"), {"i": incident.id}
        )

    with pytest.raises(DBAPIError):
        _db(settings, delete)  # the app role has no DELETE on the timeline


# ------------------------------------------------------------------ API


def test_incident_api_roles_transitions_severity_comments_and_paging(
    settings: Settings, t0: datetime
) -> None:
    svc = entity(settings, EntityType.SERVICE, "checkout")
    sids = [
        signal(
            settings,
            at=t0 + timedelta(minutes=20 * i),
            entity_id=svc,
            service="checkout",
            title=f"checkout fails {i}",
        )  # fmt: skip  # noqa: RUF028
        for i in range(3)
    ]
    for i in range(3):
        correlate(settings, t0 + timedelta(minutes=20 * i, seconds=5))  # 3 separate incidents
    incidents = [incident_of(settings, s) for s in sids]
    assert len({i.id for i in incidents if i}) == 3
    inc = incidents[0]
    assert inc is not None

    viewer = seed_user(settings, role=UserRole.VIEWER)
    responder = seed_user(settings, role=UserRole.RESPONDER)
    with client_for(app_with(settings)) as client:
        assert client.get("/api/v1/incidents").status_code == 401
        v = login(client, viewer)
        page = client.get("/api/v1/incidents", params={"service": "checkout", "limit": 2}).json()
        assert [p["number"] for p in page] == sorted(
            [i.number for i in incidents if i], reverse=True
        )[:2]
        rest = client.get(
            "/api/v1/incidents", params={"service": "checkout", "cursor": page[-1]["number"]}
        ).json()
        assert [p["number"] for p in rest] == [inc.number]
        assert rest[0]["ref"] == f"INC-{inc.number}"
        detail = client.get(f"/api/v1/incidents/{inc.id}").json()
        assert [s["reason"] for s in detail["signals"]] == ["first_signal"]
        assert detail["investigation"] is None
        r = client.patch(f"/api/v1/incidents/{inc.id}", json={"status": "investigating"},
                         headers=v.apply(client))  # fmt: skip
        assert r.status_code == 403

        rs = login(client, responder)
        h = rs.apply(client)
        r = client.patch(f"/api/v1/incidents/{inc.id}",
                         json={"status": "investigating", "note": "looking"}, headers=h)  # fmt: skip  # noqa: E501
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "investigating"
        r = client.patch(f"/api/v1/incidents/{inc.id}", json={"status": "closed"}, headers=h)
        assert r.status_code == 409
        assert "investigating to closed" in r.json()["detail"]
        r = client.patch(f"/api/v1/incidents/{inc.id}", json={"severity": "SEV4"}, headers=h)
        assert r.json()["severity"] == "SEV4"  # a person can lower it
        r = client.patch(f"/api/v1/incidents/{inc.id}", json={"status": "resolved"}, headers=h)
        assert r.json()["resolved_at"] is not None
        r = client.post(f"/api/v1/incidents/{inc.id}/comments", json={"text": "rolled back"},
                        headers=h)  # fmt: skip
        assert r.status_code == 201
        r = client.patch(f"/api/v1/incidents/{inc.id}", json={"oops": 1}, headers=h)
        assert r.status_code == 422

        events_ = client.get(f"/api/v1/incidents/{inc.id}/timeline").json()
        # System events carry this test's far-future "now"; people's carry real time.
        assert "opened" in [e["type"] for e in events_]
        mine = [e["type"] for e in events_ if e["actor"].startswith("user:")]
        assert mine == ["status_changed", "severity_changed", "status_changed", "comment"]
        assert [e["ts"] for e in events_] == sorted(e["ts"] for e in events_)
        moves = [
            (e["data"]["from"], e["data"]["to"]) for e in events_ if e["type"] == "status_changed"
        ]
        assert moves == [("open", "investigating"), ("investigating", "resolved")]
        assert all(e["actor"].startswith("user:") for e in events_ if e["type"] == "comment")
        assert client.get(f"/api/v1/incidents/{uuid.uuid4()}").status_code == 404

    audited = _db(settings, lambda db: db.scalar(text(
        "SELECT count(*) FROM audit_log WHERE action = 'incident.updated' AND target_id = :i"
    ), {"i": str(inc.id)}))  # fmt: skip
    assert audited == 3  # the 409 and the 422 changed nothing and left no record


def test_trace_dependencies_become_uses_relations_within_an_environment(settings: Settings) -> None:
    from sentinel.integrations.entities import store_dependencies

    tag = uuid.uuid4().hex[:6]
    caller = entity(settings, EntityType.SERVICE, f"gw-{tag}", **{
        "service.name": f"gw-{tag}", "deployment.environment.name": "dev"})  # fmt: skip
    callee = entity(settings, EntityType.SERVICE, f"api-{tag}", **{
        "service.name": f"api-{tag}", "deployment.environment.name": "dev"})  # fmt: skip
    prod_callee = entity(settings, EntityType.SERVICE, f"api-{tag}", **{
        "service.name": f"api-{tag}", "deployment.environment.name": "prod"})  # fmt: skip

    async def go(db: Any) -> list[tuple[uuid.UUID, uuid.UUID, str, str]]:
        written = await store_dependencies(
            db, [(f"gw-{tag}", f"api-{tag}", "dev"), (f"gw-{tag}", "nobody", None)]
        )
        assert written == 1
        rows = await db.execute(
            select(EntityRelation.from_entity_id, EntityRelation.to_entity_id,
                   EntityRelation.relation, EntityRelation.source)
            .where(EntityRelation.from_entity_id == caller)
        )  # fmt: skip
        return [tuple(r) for r in rows.all()]

    assert _db(settings, go) == [(caller, callee, "uses", "traces")]
    assert prod_callee != callee


def test_a_deployment_stamped_at_the_end_of_its_rollout_or_reported_late_is_linked(
    settings: Settings, t0: datetime
) -> None:
    svc = entity(settings, EntityType.SERVICE, "basket", **{"service.name": "basket"})
    # Errors start during the rollout; the script stamps the deployment 30 s later.
    stamped = deploy(settings, "basket", t0 + timedelta(seconds=30), uuid.uuid4().hex[:12])
    s1 = signal(settings, at=t0, entity_id=svc, service="basket", title="basket fails")
    correlate(settings, t0 + timedelta(minutes=1))
    assert incident_of(settings, s1).suspected_deployment_id == stamped  # type: ignore[union-attr]

    # A CI job reports its deployment only after the incident opened.
    other = entity(settings, EntityType.SERVICE, "wishlist", **{"service.name": "wishlist"})
    s2 = signal(settings, at=t0 + timedelta(minutes=30), entity_id=other, service="wishlist",
                title="wishlist fails")  # fmt: skip
    correlate(settings, t0 + timedelta(minutes=31))
    incident = incident_of(settings, s2)
    assert incident is not None
    assert incident.suspected_deployment_id is None
    late = deploy(settings, "wishlist", t0 + timedelta(minutes=25), uuid.uuid4().hex[:12])
    result = correlate(settings, t0 + timedelta(minutes=33))
    assert result.linked == [incident.ref]
    assert incident_of(settings, s2).suspected_deployment_id == late  # type: ignore[union-attr]
    assert [e.type for e in events(settings, incident.id)].count("deployment") == 1


def test_an_alert_alertmanager_lost_is_resolved_on_the_second_missing_pass(
    settings: Settings, t0: datetime
) -> None:
    from sentinel.detection.signals import AlertReconciler

    svc = entity(settings, EntityType.SERVICE, "ghost")
    lost = signal(settings, at=t0, entity_id=svc, service="ghost", title="ghost fails")
    kept = signal(settings, at=t0, entity_id=svc, service="ghost", title="ghost slow")

    async def fingerprints(db: Any) -> dict[uuid.UUID, str]:
        rows = await db.execute(
            select(Signal.id, Signal.fingerprint).where(Signal.id.in_([lost, kept]))
        )
        return dict(rows.all())

    fp = _db(settings, fingerprints)

    def am(*fps: str) -> list[dict[str, Any]]:
        return [{"fingerprint": f, "status": {"state": "active"}, "labels": {"alertname": "X"},
                 "startsAt": t0.isoformat()} for f in fps]  # fmt: skip

    answers: list[Any] = [am(fp[kept]), RuntimeError, am(fp[kept]), am(fp[kept])]

    async def fetch() -> list[dict[str, Any]]:
        answer = answers.pop(0)
        if answer is RuntimeError:
            import httpx

            raise httpx.ConnectError("alertmanager is down")
        return answer  # type: ignore[no-any-return]

    reconciler = AlertReconciler(fetch)
    later = t0 + timedelta(minutes=10)

    def run() -> list[uuid.UUID]:
        result: list[uuid.UUID] = _db(settings, lambda db: reconciler.run(db, now=later))
        return result

    assert lost not in run()  # missing once: not yet
    assert lost not in run()  # Alertmanager unreachable: no evidence, start over
    assert lost not in run()  # missing once again
    assert lost in run()  # missing twice in a row: resolved

    async def status(db: Any) -> dict[uuid.UUID, tuple[SignalStatus, str | None]]:
        rows = await db.execute(
            select(Signal.id, Signal.status, Signal.payload).where(Signal.id.in_([lost, kept]))
        )
        return {i: (s, p.get("resolved_because")) for i, s, p in rows.all()}

    states = _db(settings, status)
    assert states[lost] == (SignalStatus.RESOLVED, "no longer active in Alertmanager")
    assert states[kept][0] is SignalStatus.FIRING


def test_an_alert_the_webhook_never_delivered_is_taken_from_alertmanager(
    settings: Settings, t0: datetime
) -> None:
    from sentinel.detection.signals import AlertReconciler

    fingerprint = uuid.uuid4().hex[:16]
    service = f"silent-{uuid.uuid4().hex[:6]}"
    alert = {
        "fingerprint": fingerprint,
        "status": {"state": "active"},
        "labels": {"alertname": "ServiceHighErrorRatio", "severity": "critical",
                   "service_name": service},
        "annotations": {"summary": f"{service}: 100% of requests fail", "value": "1"},
        "startsAt": t0.isoformat(),
        "generatorURL": "http://prometheus/graph",
    }  # fmt: skip
    suppressed = {"fingerprint": uuid.uuid4().hex[:16], "status": {"state": "suppressed"},
                  "labels": {"alertname": "Y"}, "startsAt": t0.isoformat()}  # fmt: skip

    async def fetch() -> list[dict[str, Any]]:
        return [alert, suppressed]

    async def taken(db: Any) -> list[Signal]:
        return list((await db.scalars(select(Signal).where(Signal.service_name == service))).all())

    reconciler = AlertReconciler(fetch)
    _db(settings, lambda db: reconciler.run(db, now=t0 + timedelta(seconds=30)))
    assert _db(settings, taken) == []  # too young: the webhook's to deliver
    for _ in range(2):  # a second pass finds it firing: nothing more
        _db(settings, lambda db: reconciler.run(db, now=t0 + timedelta(minutes=3)))

    (signal_,) = _db(settings, taken)
    assert signal_.status is SignalStatus.FIRING
    assert signal_.fingerprint == fingerprint
    assert signal_.title == f"{service}: 100% of requests fail"


def test_automatic_reassessment_never_lowers_severity(settings: Settings, t0: datetime) -> None:
    from sentinel.incidents import service as incidents

    svc = entity(settings, EntityType.SERVICE, "pricing")
    first = signal(settings, at=t0, entity_id=svc, service="pricing", title="pricing slow",
                   severity="warning")  # fmt: skip
    correlate(settings, t0 + timedelta(seconds=5))
    incident = incident_of(settings, first)
    assert incident is not None
    assert incident.severity is Severity.SEV3

    async def raise_it(db: Any) -> None:  # a person: "this is a SEV1"
        await incidents.set_severity(db, await db.get(Incident, incident.id), Severity.SEV1,
                                     actor="user:test")  # fmt: skip

    _db(settings, raise_it)
    signal(settings, at=t0 + timedelta(minutes=1), entity_id=svc, service="pricing",
           title="pricing slow again", severity="warning")  # fmt: skip
    correlate(settings, t0 + timedelta(minutes=2))  # the rules alone say SEV3
    assert incident_of(settings, first).severity is Severity.SEV1  # type: ignore[union-attr]


def test_a_host_alert_is_about_the_host_not_the_agent_that_reported_it(settings: Settings) -> None:
    """Host metrics carry the agent's `job`; the alert must still be the host's (seen in CI)."""
    from sentinel.integrations.entities import resolve_entity

    tag = uuid.uuid4().hex[:6]

    async def go(db: Any) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]:
        host = Entity(type=EntityType.HOST, key=f"vm-{tag}", display_name=f"vm-{tag}")
        agent = Entity(type=EntityType.SERVICE, key=f"sentinel/agent-{tag}@ci", display_name="agent",  # noqa: E501
                       attributes={"service.name": f"agent-{tag}", "service.namespace": "sentinel"})  # fmt: skip  # noqa: E501
        db.add_all([host, agent])
        await db.flush()
        labels = {"alertname": "HostFilesystemFull", "entity_type": "host",
                  "host_name": f"vm-{tag}", "job": f"sentinel/agent-{tag}"}  # fmt: skip
        as_host = await resolve_entity(db, labels)
        # Without entity_type (e.g. a hand-written rule), the job still names a service.
        legacy = await resolve_entity(db, {k: v for k, v in labels.items() if k != "entity_type"})
        return host.id, as_host, legacy

    host, as_host, legacy = _db(settings, go)
    assert as_host == host
    assert legacy != host  # the old behaviour, kept for alerts that don't say
