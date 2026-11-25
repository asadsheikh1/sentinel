"""The live setup checklist (Step 14): what ticks, when, per integration kind."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.auth.service import Actor
from sentinel.config import Settings
from sentinel.db.engine import create_session_factory
from sentinel.db.models import (
    CheckKind,
    Entity,
    EntityType,
    HealthCheck,
    IngestToken,
    Integration,
    IntegrationKind,
    IntegrationSignalStatus,
    ProbeState,
    UserRole,
)
from sentinel.integrations.service import IntegrationService
from sentinel.integrations.verification import checklist

from .authkit import app_with, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(migrated.url("sentinel_app"))


def _db(settings: Settings, fn: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
        try:
            async with create_session_factory(engine)() as db, db.begin():
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _create(settings: Settings, kind: IntegrationKind, signals: list[str]) -> uuid.UUID:
    async def go(db: Any) -> uuid.UUID:
        integration, _ = await IntegrationService(db, settings).create(
            name=f"v-{uuid.uuid4().hex[:8]}", kind=kind, environment="dev", config={},
            expected_signals=signals, rate_limit_per_min=None,
            actor=Actor.system("test"), created_by=None,
        )  # fmt: skip
        return integration.id

    result: uuid.UUID = _db(settings, go)
    return result


def _items(settings: Settings, iid: uuid.UUID, **lookups: Any) -> dict[str, Any]:
    async def go(db: Any) -> dict[str, Any]:
        integration = await db.get(Integration, iid)
        items = await checklist(db, integration, now=NOW, **lookups)
        return {i.key: i for i in items}

    result: dict[str, Any] = _db(settings, go)
    return result


def test_an_agent_checklist_ticks_as_each_part_of_the_setup_works(settings: Settings) -> None:
    iid = _create(settings, IntegrationKind.LINUX_HOST, ["metrics", "logs"])
    marker = {"seen": False, "asked": 0}

    async def marker_seen(_: uuid.UUID) -> bool:
        marker["asked"] += 1
        return bool(marker["seen"])

    async def hashes(_: uuid.UUID) -> set[str]:
        return {"old-hash"}

    lookups = {"marker_seen": marker_seen, "reported_hashes": hashes, "expected_hash": "new-hash"}
    items = _items(settings, iid, **lookups)
    assert list(items) == [
        "token_used",
        "signal:metrics",
        "signal:logs",
        "marker",
        "host",
        "config",
    ]
    assert not any(i.done for i in items.values())
    assert items["config"].required is False
    assert items["config"].detail == "out of date: re-run the installer to update it"

    # The agent starts: token used, metrics and logs flow, the marker and the host arrive.
    async def arrive(db: Any) -> None:
        token = (await db.execute(
            IngestToken.__table__.select().where(IngestToken.integration_id == iid)
        )).first()  # fmt: skip
        await db.execute(
            IngestToken.__table__.update()
            .where(IngestToken.id == token.id)
            .values(last_used_at=NOW)
        )
        for signal in ("metrics", "logs"):
            db.add(IntegrationSignalStatus(integration_id=iid, signal=signal, last_seen_at=NOW))
        db.add(Entity(type=EntityType.HOST, key=f"vm-{iid.hex[:6]}", display_name="vm-1",
                      integration_ids=[iid]))  # fmt: skip

    _db(settings, arrive)
    marker["seen"] = True
    items = _items(settings, iid, **{**lookups, "reported_hashes": _const({"new-hash"})})
    assert all(i.done for i in items.values()), {k: i.done for k, i in items.items()}
    assert items["host"].detail == "vm-1"

    # Once seen, the marker is remembered: Loki isn't asked again.
    asked = marker["asked"]
    marker["seen"] = False
    assert _items(settings, iid, **lookups)["marker"].done
    assert marker["asked"] == asked


def _const(value: set[str]) -> Any:
    async def fn(_: uuid.UUID) -> set[str]:
        return value

    return fn


def test_an_unreachable_store_leaves_items_unticked_not_an_error(settings: Settings) -> None:
    iid = _create(settings, IntegrationKind.CONTAINER_HOST, ["metrics"])

    async def down(_: uuid.UUID) -> Any:
        raise ConnectionError("loki is down")

    items = _items(settings, iid, marker_seen=down, reported_hashes=down, expected_hash="h")
    assert items["marker"].done is False
    assert items["config"].done is False
    assert items["config"].detail == "not reported yet"
    assert items["containers"].required is False


def test_http_check_and_app_checklists(settings: Settings) -> None:
    check_iid = _create(settings, IntegrationKind.HTTP_CHECK, ["metrics"])
    items = _items(settings, check_iid)
    assert list(items) == ["check", "probe"]
    assert not items["check"].done

    async def add_check(db: Any) -> None:
        db.add(HealthCheck(integration_id=check_iid, name="shop", kind=CheckKind.HTTP,
                           target="https://shop.example.com/", interval_s=60, timeout_s=5,
                           assertions={}, state=ProbeState.UP, last_result_at=NOW))  # fmt: skip

    _db(settings, add_check)
    items = _items(settings, check_iid)
    assert items["check"].done
    assert items["probe"].done
    assert items["probe"].detail == "shop: up"

    app_iid = _create(settings, IntegrationKind.OTEL_APP, ["traces", "logs"])
    items = _items(settings, app_iid)
    assert list(items) == ["token_used", "signal:logs", "signal:traces", "services"]


def test_the_status_api_returns_the_checklist_to_any_role(settings: Settings) -> None:
    iid = _create(settings, IntegrationKind.LINUX_HOST, ["metrics", "logs"])
    viewer = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        login(client, viewer)
        body = client.get(f"/api/v1/integrations/{iid}/status").json()
        # No Loki or Prometheus in this test: those items just aren't ticked.
        assert [i["key"] for i in body["checklist"]] == [
            "token_used", "signal:metrics", "signal:logs", "marker", "host", "config",
        ]  # fmt: skip
        assert not any(i["done"] for i in body["checklist"])
        entities = client.get("/api/v1/entities", params={"integration": str(iid)})
        assert entities.status_code == 200
        assert entities.json() == []
        meta = client.get("/api/v1/meta").json()
        assert meta["ingest_endpoint"] == settings.agent_public_endpoint
