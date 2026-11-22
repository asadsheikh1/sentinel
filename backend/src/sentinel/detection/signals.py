"""Signals: what detection produces. Stored once, updated as they resolve.

A rule-pack alert becomes one signal per *episode*: Alertmanager's fingerprint
(the label set) plus the time it started firing. Re-deliveries of the same
episode (Alertmanager repeats and retries) update it; the same alert firing
again later is a new signal.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import Integration, Signal, SignalKind, SignalStatus
from sentinel.integrations.entities import resolve_entity
from sentinel.security.redaction import clean_value

SEVERITIES = ("critical", "warning", "info")
# Alertmanager sends this for "not resolved yet".
_ZERO_TIME = datetime(1, 1, 1, tzinfo=UTC)


class AlertIn(BaseModel):
    """One alert of an Alertmanager webhook (payload version 4)."""

    model_config = ConfigDict(extra="ignore")

    status: str = Field(pattern=r"^(firing|resolved)$")
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime  # noqa: N815 - Alertmanager's field names
    endsAt: datetime | None = None  # noqa: N815
    generatorURL: str = ""  # noqa: N815
    fingerprint: str = Field(pattern=r"^[0-9a-f]{1,64}$")


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = Field(pattern=r"^4$")
    status: str
    receiver: str = ""
    alerts: list[AlertIn] = Field(max_length=1000)


def _severity(labels: Mapping[str, str]) -> str:
    value = labels.get("severity", "warning").lower()
    return value if value in SEVERITIES else "warning"


async def _labels_integration(db: AsyncSession, labels: Mapping[str, str]) -> uuid.UUID | None:
    """The integration named in the labels, if it (still) exists."""
    try:
        integration_id = uuid.UUID(labels.get("sentinel_integration_id", ""))
    except ValueError:
        return None
    found: uuid.UUID | None = await db.scalar(
        select(Integration.id).where(Integration.id == integration_id)
    )
    return found


async def upsert_alert(db: AsyncSession, alert: AlertIn) -> tuple[Signal, str]:
    """Store or update one alert. Returns (signal, "created" | "updated" | "unchanged")."""
    labels = clean_value(alert.labels)
    annotations = clean_value(alert.annotations)
    key = f"am:{alert.fingerprint}:{int(alert.startsAt.timestamp())}"
    status = SignalStatus(alert.status)
    ends_at = alert.endsAt if alert.endsAt and alert.endsAt > _ZERO_TIME else None
    if status is SignalStatus.FIRING:
        ends_at = None  # Alertmanager sends a future "endsAt" while firing
    signal = await db.scalar(select(Signal).where(Signal.dedupe_key == key).with_for_update())
    payload = {"annotations": annotations, "generator_url": alert.generatorURL}
    if signal is None:
        signal = Signal(
            kind=SignalKind.PROM_ALERT,
            status=status,
            severity=_severity(labels),
            title=str(annotations.get("summary") or labels.get("alertname", "alert")),
            entity_id=await resolve_entity(db, labels),
            service_name=labels.get("service_name"),
            source_integration_id=await _labels_integration(db, labels),
            fingerprint=alert.fingerprint,
            labels=labels,
            payload=payload,
            starts_at=alert.startsAt,
            ends_at=ends_at,
            dedupe_key=key,
        )
        db.add(signal)
        await db.flush()
        return signal, "created"
    if signal.status is status and signal.ends_at == ends_at:
        return signal, "unchanged"
    if signal.status is SignalStatus.RESOLVED and status is SignalStatus.FIRING:
        return signal, "unchanged"  # a late, out-of-order "firing" never re-opens
    signal.status = status
    signal.ends_at = ends_at
    signal.payload = payload
    signal.updated_at = datetime.now(UTC)
    await db.flush()
    return signal, "updated"


async def ingest_alertmanager(db: AsyncSession, webhook: AlertmanagerWebhook) -> dict[str, int]:
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for alert in webhook.alerts:
        _, outcome = await upsert_alert(db, alert)
        counts[outcome] += 1
    return counts


async def list_signals(
    db: AsyncSession,
    *,
    status: SignalStatus | None = None,
    kind: SignalKind | None = None,
    service: str | None = None,
    entity: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> Sequence[Signal]:
    stmt = select(Signal).order_by(Signal.starts_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Signal.status == status)
    if kind is not None:
        stmt = stmt.where(Signal.kind == kind)
    if service is not None:
        stmt = stmt.where(Signal.service_name == service)
    if entity is not None:
        stmt = stmt.where(Signal.entity_id == entity)
    if since is not None:
        stmt = stmt.where(Signal.starts_at >= since)
    return (await db.scalars(stmt)).all()


def as_dict(signal: Signal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "kind": signal.kind,
        "status": signal.status,
        "severity": signal.severity,
        "title": signal.title,
        "entity_id": signal.entity_id,
        "service_name": signal.service_name,
        "source_integration_id": signal.source_integration_id,
        "labels": signal.labels,
        "payload": signal.payload,
        "starts_at": signal.starts_at,
        "ends_at": signal.ends_at,
        "received_at": signal.received_at,
    }
