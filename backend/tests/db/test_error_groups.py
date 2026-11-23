"""Error grouping with a real Postgres and a fake Loki: S01, restart safety, spikes, API."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.config import Settings
from sentinel.db.engine import create_session_factory
from sentinel.db.models import (
    Entity,
    EntityType,
    ErrorGroup,
    PollWatermark,
    Signal,
    SignalKind,
    SignalStatus,
    UserRole,
)
from sentinel.detection import poller
from sentinel.detection.poller import LOCK_KEY, poll_pass
from sentinel.telemetry.base import TelemetryError
from sentinel.telemetry.loki import LokiClient
from tests.fake_loki import FakeLoki

from .authkit import app_with, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db

STACK = (
    Path(__file__).resolve().parents[1] / "fixtures" / "tracebacks" / "aviro_s01_keyerror.txt"
).read_text()
T0 = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(
        migrated.url("sentinel_app"),
        error_poll_lag_s=0,
        error_poll_lookback_s=3 * 3600,
        error_group_quiet_s=600,
    )


def _ns(at: datetime) -> int:
    return int(at.timestamp() * 1e9)


def _run(settings: Settings, fn: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _poll(settings: Settings, fake: FakeLoki, now: datetime, source: str) -> poller.PassResult:
    async def go(factory: Any) -> poller.PassResult:
        return await poll_pass(
            factory,
            settings,
            loki_factory=lambda: LokiClient("http://loki", transport=fake.transport),
            now=now,
            source=source,
        )

    result: poller.PassResult = _run(settings, go)
    return result


def _s01(fake: FakeLoki, service: str, start: datetime, n: int, *, step_s: float = 1.0) -> None:
    for i in range(n):
        fake.add(
            _ns(start + timedelta(seconds=i * step_s)),
            "unhandled exception",
            service_name=service,
            service_namespace="aviro",
            deployment_environment_name="dev",
            severity_number="17",
            severity_text="ERROR",
            exception_type="KeyError",
            exception_message="gift_message",
            exception_stacktrace=STACK,
            code_file_path="/app/.venv/lib/python3.12/site-packages/aviro/common/service.py",
            code_function_name="faults_and_errors",
            scope_name="aviro.orders",
            trace_id=uuid.uuid4().hex,
            order_id=str(uuid.uuid4()),  # high-cardinality: a different stream per line
        )


def _groups(settings: Settings, service: str) -> list[ErrorGroup]:
    async def go(factory: Any) -> list[ErrorGroup]:
        async with factory() as db:
            rows = await db.scalars(select(ErrorGroup).where(ErrorGroup.service_name == service))
            return list(rows.all())

    result: list[ErrorGroup] = _run(settings, go)
    return result


def _signals(settings: Settings, service: str) -> list[Signal]:
    async def go(factory: Any) -> list[Signal]:
        async with factory() as db:
            rows = await db.scalars(
                select(Signal)
                .where(Signal.service_name == service, Signal.kind != SignalKind.PROM_ALERT)
                .order_by(Signal.starts_at)
            )
            return list(rows.all())

    result: list[Signal] = _run(settings, go)
    return result


def _seed_service(settings: Settings, service: str) -> uuid.UUID:
    async def go(factory: Any) -> uuid.UUID:
        async with factory() as db, db.begin():
            e = Entity(
                type=EntityType.SERVICE,
                key=f"aviro/{service}@dev",
                display_name=service,
                attributes={
                    "service.name": service,
                    "service.namespace": "aviro",
                    "deployment.environment.name": "dev",
                },
            )
            db.add(e)
            await db.flush()
            return e.id

    result: uuid.UUID = _run(settings, go)
    return result


def _source() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------ S01


def test_s01_is_exactly_one_new_error_group_with_the_right_top_frame(settings: Settings) -> None:
    service = f"orders-{uuid.uuid4().hex[:6]}"
    entity = _seed_service(settings, service)
    fake, source = FakeLoki(), _source()
    _s01(fake, service, T0, 40)

    result = _poll(settings, fake, T0 + timedelta(minutes=5), source)
    assert result.applied is not None
    assert result.applied.groups_new == 1
    (group,) = _groups(settings, service)
    assert group.exception_type == "KeyError"
    assert group.top_frame == "aviro/orders/app.py:create_order"
    assert group.total_count == 40
    assert group.entity_id == entity
    assert group.source_integration_id is None  # the label names no real integration
    assert len(group.sample_trace_ids) == 5
    assert "create_order" in group.sample_log["stacktrace"]

    (signal,) = _signals(settings, service)
    assert signal.kind is SignalKind.ERROR_GROUP_NEW
    assert signal.status is SignalStatus.FIRING
    assert signal.entity_id == entity
    assert signal.fingerprint == group.fingerprint
    assert "KeyError" in signal.title
    assert "aviro/orders/app.py:create_order" in signal.title

    # More of the same later: the same group, no new signal.
    _s01(fake, service, T0 + timedelta(minutes=6), 10)
    _poll(settings, fake, T0 + timedelta(minutes=8), source)
    (group,) = _groups(settings, service)
    assert group.total_count == 50
    assert len(_signals(settings, service)) == 1

    # Quiet for 10 minutes: the "new error" signal resolves; the group stays.
    _poll(settings, fake, T0 + timedelta(minutes=20), source)
    (signal,) = _signals(settings, service)
    assert signal.status is SignalStatus.RESOLVED
    assert signal.payload["resolved_because"].startswith("no occurrences")


def test_a_pass_repeated_over_the_same_window_counts_nothing_twice(settings: Settings) -> None:
    service = f"orders-{uuid.uuid4().hex[:6]}"
    fake, source = FakeLoki(), _source()
    _s01(fake, service, T0, 30)
    now = T0 + timedelta(minutes=2)
    _poll(settings, fake, now, source)
    _poll(settings, fake, now, source)  # same "now": nothing new to read
    _poll(settings, fake, now + timedelta(seconds=30), source)
    (group,) = _groups(settings, service)
    assert group.total_count == 30


# ------------------------------------------------------------------ restart safety


def test_a_failed_pass_commits_nothing_and_the_retry_counts_once(settings: Settings) -> None:
    service = f"orders-{uuid.uuid4().hex[:6]}"
    fake, source = FakeLoki(fail_on=2), _source()  # the 2nd query of the pass fails
    _s01(fake, service, T0, 25)
    with pytest.raises(TelemetryError):
        _poll(settings, fake, T0 + timedelta(minutes=2), source)
    assert _groups(settings, service) == []
    assert _watermark(settings, source) is None

    fake.fail_on = None
    _poll(settings, fake, T0 + timedelta(minutes=2), source)
    (group,) = _groups(settings, service)
    assert group.total_count == 25


def test_a_crash_after_counting_commits_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = f"orders-{uuid.uuid4().hex[:6]}"
    fake, source = FakeLoki(), _source()
    _s01(fake, service, T0, 25)
    real_apply = poller.apply

    async def apply_then_crash(*args: Any, **kwargs: Any) -> Any:
        await real_apply(*args, **kwargs)
        raise RuntimeError("process killed")

    monkeypatch.setattr(poller, "apply", apply_then_crash)
    with pytest.raises(RuntimeError):
        _poll(settings, fake, T0 + timedelta(minutes=2), source)
    assert _groups(settings, service) == []
    assert _signals(settings, service) == []

    monkeypatch.setattr(poller, "apply", real_apply)
    _poll(settings, fake, T0 + timedelta(minutes=2), source)
    (group,) = _groups(settings, service)
    assert group.total_count == 25
    assert len(_signals(settings, service)) == 1


def test_small_pages_and_a_page_budget_count_exactly_the_same(migrated: Pg) -> None:
    settings = make_settings(
        migrated.url("sentinel_app"),
        error_poll_lag_s=0,
        error_poll_lookback_s=3600,
        error_poll_page_size=10,
        error_poll_max_pages=2,
    )
    service = f"orders-{uuid.uuid4().hex[:6]}"
    fake, source = FakeLoki(), _source()
    _s01(fake, service, T0, 95, step_s=0.5)
    for i in range(6):  # lines that share a nanosecond, across page boundaries
        fake.add(_ns(T0 + timedelta(seconds=10)), "unhandled exception", **{
            **fake.entries[0].labels, "trace_id": f"same-{i}"})  # fmt: skip
    now = T0 + timedelta(minutes=2)
    passes = 0
    while (mark := _watermark(settings, source)) is None or mark < _ns(now):
        _poll(settings, fake, now, source)
        passes += 1
        assert passes < 30
    assert passes > 1  # the budget really did split the work
    (group,) = _groups(settings, service)
    assert group.total_count == 101


def _watermark(settings: Settings, source: str) -> int | None:
    async def go(factory: Any) -> int | None:
        async with factory() as db:
            mark = await db.get(PollWatermark, source)
            return mark.last_ns if mark else None

    result: int | None = _run(settings, go)
    return result


def test_a_second_poller_skips_while_the_first_holds_the_lock(settings: Settings) -> None:
    fake = FakeLoki()

    async def go(factory: Any) -> poller.PassResult:
        async with factory() as other, other.begin():
            await other.execute(select(func.pg_advisory_xact_lock(LOCK_KEY)))
            return await poll_pass(
                factory,
                settings,
                loki_factory=lambda: LokiClient("http://loki", transport=fake.transport),
                now=T0,
                source=_source(),
            )

    result: poller.PassResult = _run(settings, go)
    assert result.skipped == "another poller holds the lock"
    assert fake.requests == 0


# ------------------------------------------------------------------ spikes


def test_a_known_error_that_spikes_raises_a_spike_signal_that_resolves(settings: Settings) -> None:
    service = f"orders-{uuid.uuid4().hex[:6]}"
    fake, source = FakeLoki(), _source()
    start = T0 - timedelta(minutes=80)
    _s01(fake, service, start, 75, step_s=60)  # one a minute: its normal
    _poll(settings, fake, T0 - timedelta(minutes=4), source)
    assert [s.kind for s in _signals(settings, service)] == [SignalKind.ERROR_GROUP_NEW]

    _s01(fake, service, T0 - timedelta(minutes=2), 40, step_s=3)  # 40 in 2 minutes
    _poll(settings, fake, T0, source)
    spikes = [s for s in _signals(settings, service) if s.kind is SignalKind.ERROR_GROUP_SPIKE]
    assert len(spikes) == 1
    assert spikes[0].status is SignalStatus.FIRING
    assert spikes[0].payload["count_5m"] >= 40
    assert "error spike" in spikes[0].title

    _poll(settings, fake, T0 + timedelta(minutes=8), source)  # quiet again
    spikes = [s for s in _signals(settings, service) if s.kind is SignalKind.ERROR_GROUP_SPIKE]
    assert [s.status for s in spikes] == [SignalStatus.RESOLVED]


# ------------------------------------------------------------------ API


def test_error_groups_api(settings: Settings) -> None:
    service = f"orders-{uuid.uuid4().hex[:6]}"
    fake, source = FakeLoki(), _source()
    _s01(fake, service, T0, 12)
    _poll(settings, fake, T0 + timedelta(minutes=2), source)
    viewer = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        assert client.get("/api/v1/error-groups").status_code == 401
        login(client, viewer)
        rows = client.get("/api/v1/error-groups", params={"service": service}).json()
        assert len(rows) == 1
        assert rows[0]["top_frame"] == "aviro/orders/app.py:create_order"
        assert rows[0]["total_count"] == 12
        detail = client.get(f"/api/v1/error-groups/{rows[0]['id']}").json()
        assert "create_order" in detail["sample_log"]["stacktrace"]
        assert [s["kind"] for s in detail["signals"]] == ["error_group_new"]
        assert sum(detail["minute_counts"].values()) == 12
        assert client.get(f"/api/v1/error-groups/{uuid.uuid4()}").status_code == 404
        signals = client.get(
            "/api/v1/signals", params={"service": service, "kind": "error_group_new"}
        ).json()
        assert len(signals) == 1
