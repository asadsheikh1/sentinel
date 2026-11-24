"""The incident service: open, attach, re-assess, transition, comment. All on the timeline.

Every change writes an ``incident_events`` row in the same transaction. The
correlator (``detection/correlator.py``) decides *which* incident a signal
belongs to; this module does the bookkeeping.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.config import Settings
from sentinel.db.models import (
    Deployment,
    Entity,
    Incident,
    IncidentEntity,
    IncidentEvent,
    IncidentSignal,
    IncidentStatus,
    Severity,
    Signal,
    SignalStatus,
)
from sentinel.incidents import investigation
from sentinel.incidents.lifecycle import check_transition
from sentinel.incidents.severity import Assessment, SignalFacts, assess, rank

log = structlog.get_logger("sentinel.incidents")

SYSTEM = "system"
MAX_COMMENT = 5000
ROLLOUT_GRACE = timedelta(minutes=2)
LATE_LINK = timedelta(minutes=30)  # keep looking for a late deployment event this long


def user_actor(user_id: uuid.UUID | str) -> str:
    return f"user:{user_id}"


def _event(
    incident: Incident, type_: str, actor: str, data: dict[str, Any], ts: datetime
) -> IncidentEvent:
    return IncidentEvent(incident_id=incident.id, type=type_, actor=actor, data=data, ts=ts)


def facts(signal: Signal) -> SignalFacts:
    value = signal.payload.get("annotations", {}).get("value") if signal.payload else None
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        number = None
    return SignalFacts(
        severity=signal.severity, alertname=signal.labels.get("alertname"), value=number
    )


# ------------------------------------------------------------------ queries


async def signals_of(db: AsyncSession, incident_id: uuid.UUID) -> list[tuple[Signal, str]]:
    rows = await db.execute(
        select(Signal, IncidentSignal.reason)
        .join(IncidentSignal, IncidentSignal.signal_id == Signal.id)
        .where(IncidentSignal.incident_id == incident_id)
        .order_by(Signal.starts_at)
    )
    return [(s, r) for s, r in rows.all()]


async def entities_of(db: AsyncSession, incident_id: uuid.UUID) -> list[tuple[Entity, str]]:
    rows = await db.execute(
        select(Entity, IncidentEntity.role)
        .join(IncidentEntity, IncidentEntity.entity_id == Entity.id)
        .where(IncidentEntity.incident_id == incident_id)
        .order_by(IncidentEntity.added_at)
    )
    return [(e, r) for e, r in rows.all()]


async def timeline(db: AsyncSession, incident_id: uuid.UUID) -> list[IncidentEvent]:
    rows = await db.scalars(
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident_id)
        .order_by(IncidentEvent.ts, IncidentEvent.id)
    )
    return list(rows.all())


async def assessment(db: AsyncSession, incident: Incident) -> Assessment:
    signals = [s for s, _ in await signals_of(db, incident.id)]
    entities = [e for e, _ in await entities_of(db, incident.id)]
    services = {s.service_name for s in signals if s.service_name}
    return assess(
        [facts(s) for s in signals],
        services=len(services),
        criticalities=[e.criticality for e in entities],
    )


async def nearest_deployment(
    db: AsyncSession, *, service: str | None, entity_ids: Sequence[uuid.UUID], before: datetime,
    lookback: timedelta,
) -> Deployment | None:  # fmt: skip
    """The latest deployment of the incident's service or entities in the window before it.

    Up to ``ROLLOUT_GRACE`` *after* ``before`` counts too: a deploy script or CI
    job usually stamps the deployment when the rollout ends, but its errors
    start during the rollout.
    """
    conditions = []
    if service:
        conditions.append(Deployment.service_name == service)
    if entity_ids:
        conditions.append(Deployment.entity_id.in_(list(entity_ids)))
    if not conditions:
        return None
    found: Deployment | None = await db.scalar(
        select(Deployment)
        .where(
            or_(*conditions),
            Deployment.deployed_at <= before + ROLLOUT_GRACE,
            Deployment.deployed_at >= before - lookback,
            Deployment.status != "failed",
        )
        .order_by(Deployment.deployed_at.desc())
        .limit(1)
    )
    return found


async def release_services(db: AsyncSession, deployment: Deployment) -> set[str]:
    """Services deployed in the same release (same commit, within 10 minutes)."""
    if not deployment.commit_sha:
        return {deployment.service_name}
    rows = await db.scalars(
        select(Deployment.service_name).where(
            Deployment.commit_sha == deployment.commit_sha,
            Deployment.deployed_at.between(
                deployment.deployed_at - timedelta(minutes=10),
                deployment.deployed_at + timedelta(minutes=10),
            ),
        )
    )
    return set(rows.all())


# ------------------------------------------------------------------ changes


async def _add_entity(
    db: AsyncSession, incident: Incident, entity_id: uuid.UUID | None, role: str, now: datetime
) -> None:
    if entity_id is None:
        return
    exists = await db.get(IncidentEntity, (incident.id, entity_id))
    if exists is None:
        db.add(
            IncidentEntity(incident_id=incident.id, entity_id=entity_id, role=role, added_at=now)
        )


async def _reassess(db: AsyncSession, incident: Incident, now: datetime) -> None:
    """Severity only goes *up* automatically."""
    await db.flush()
    result = await assessment(db, incident)
    if rank(result.severity) < rank(incident.severity):
        db.add(
            _event(
                incident, "severity_changed", SYSTEM,
                {"from": incident.severity.value, "to": result.severity.value,
                 "reasons": list(result.reasons)},
                now,
            )
        )  # fmt: skip
        incident.severity = result.severity
        incident.updated_at = now


async def open_incident(
    db: AsyncSession, signal: Signal, *, settings: Settings, now: datetime | None = None
) -> Incident:
    now = now or datetime.now(UTC)
    entity = await db.get(Entity, signal.entity_id) if signal.entity_id else None
    first = assess(
        [facts(signal)],
        services=1 if signal.service_name else 0,
        criticalities=[entity.criticality] if entity is not None else [],
    )
    incident = Incident(
        title=signal.title[:300],
        status=IncidentStatus.OPEN,
        severity=first.severity,
        started_at=signal.starts_at,
        detected_at=now,
        last_signal_at=signal.starts_at,
        primary_entity_id=signal.entity_id,
        service_name=signal.service_name,
        created_by=SYSTEM,
        created_at=now,
        updated_at=now,
    )
    db.add(incident)
    await db.flush()
    await _add_entity(db, incident, signal.entity_id, "origin", now)
    db.add(IncidentSignal(incident_id=incident.id, signal_id=signal.id, reason="first_signal",
                          attached_at=now))  # fmt: skip
    db.add(
        _event(
            incident, "opened", SYSTEM,
            {"signal_id": str(signal.id), "title": signal.title, "severity": first.severity.value,
             "reasons": list(first.reasons)},
            now,
        )
    )  # fmt: skip
    db.add(_attached_event(incident, signal, "first_signal", now))

    await link_deployment(db, incident, settings=settings, now=now)

    if await investigation.enqueue(db, incident.id, trigger="auto"):
        db.add(_event(incident, "investigation_queued", SYSTEM, {"trigger": "auto"}, now))
    await db.flush()
    log.info("incident_opened", incident=incident.ref, severity=incident.severity.value)
    return incident


async def link_deployment(
    db: AsyncSession, incident: Incident, *, settings: Settings, now: datetime
) -> bool:
    """Link the nearest deployment before the incident, if there is one. True if linked."""
    if incident.suspected_deployment_id is not None:
        return False
    entity_ids = [e.id for e, _ in await entities_of(db, incident.id)]
    deployment = await nearest_deployment(
        db,
        service=incident.service_name,
        entity_ids=entity_ids,
        before=incident.started_at,
        lookback=timedelta(seconds=settings.deploy_lookback_s),
    )
    if deployment is None:
        return False
    incident.suspected_deployment_id = deployment.id
    incident.updated_at = now
    db.add(_deployment_event(incident, deployment, now))
    return True


async def link_late_deployments(
    db: AsyncSession, *, settings: Settings, now: datetime
) -> list[str]:
    """A deployment event can arrive after the incident opened (CI reports at the end)."""
    rows = await db.scalars(
        select(Incident).where(
            Incident.suspected_deployment_id.is_(None),
            Incident.status.in_([IncidentStatus.OPEN, IncidentStatus.INVESTIGATING]),
            Incident.detected_at >= now - LATE_LINK,
        )
    )
    linked = []
    for incident in rows.all():
        if await link_deployment(db, incident, settings=settings, now=now):
            linked.append(incident.ref)
    return linked


def _attached_event(
    incident: Incident, signal: Signal, reason: str, now: datetime
) -> IncidentEvent:
    return _event(
        incident, "signal_attached", SYSTEM,
        {"signal_id": str(signal.id), "kind": signal.kind.value, "title": signal.title,
         "severity": signal.severity, "reason": reason, "entity_id":
         str(signal.entity_id) if signal.entity_id else None},
        now,
    )  # fmt: skip


def _deployment_event(incident: Incident, d: Deployment, now: datetime) -> IncidentEvent:
    before = (incident.started_at - d.deployed_at).total_seconds()
    return _event(
        incident, "deployment", SYSTEM,
        {"deployment_id": str(d.id), "service": d.service_name, "version": d.version,
         "commit_sha": d.commit_sha, "deployed_at": d.deployed_at.isoformat(),
         "seconds_before_incident": int(before)},
        now,
    )  # fmt: skip


async def attach(
    db: AsyncSession,
    incident: Incident,
    signal: Signal,
    reason: str,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    db.add(IncidentSignal(incident_id=incident.id, signal_id=signal.id, reason=reason,
                          attached_at=now))  # fmt: skip
    await _add_entity(db, incident, signal.entity_id, "affected", now)
    db.add(_attached_event(incident, signal, reason, now))
    incident.last_signal_at = max(incident.last_signal_at, signal.starts_at)
    incident.started_at = min(incident.started_at, signal.starts_at)
    incident.updated_at = now
    if incident.status is IncidentStatus.MITIGATED and signal.status is SignalStatus.FIRING:
        await transition(db, incident, IncidentStatus.OPEN, actor=SYSTEM, now=now,
                         note="a new signal fired")  # fmt: skip
    await _reassess(db, incident, now)


async def transition(
    db: AsyncSession, incident: Incident, target: IncidentStatus, *, actor: str,
    now: datetime | None = None, note: str | None = None,
) -> None:  # fmt: skip
    """Raises ``InvalidTransitionError`` for a move the state machine forbids."""
    now = now or datetime.now(UTC)
    if target is incident.status:
        return
    check_transition(incident.status, target)
    data: dict[str, Any] = {"from": incident.status.value, "to": target.value}
    if note:
        data["note"] = note[:MAX_COMMENT]
    incident.status = target
    incident.updated_at = now
    if target is IncidentStatus.MITIGATED:
        incident.mitigated_at = now
    elif target is IncidentStatus.RESOLVED:
        incident.resolved_at = now
    elif target is IncidentStatus.CLOSED:
        incident.closed_at = now
    elif target is IncidentStatus.OPEN:
        incident.resolved_at = incident.closed_at = None
    db.add(_event(incident, "status_changed", actor, data, now))


async def set_severity(
    db: AsyncSession,
    incident: Incident,
    severity: Severity,
    *,
    actor: str,
    now: datetime | None = None,
) -> None:
    """A person's override: any direction."""
    now = now or datetime.now(UTC)
    if severity is incident.severity:
        return
    change = {"from": incident.severity.value, "to": severity.value, "manual": True}
    db.add(_event(incident, "severity_changed", actor, change, now))
    incident.severity = severity
    incident.updated_at = now


async def set_title(
    db: AsyncSession, incident: Incident, title: str, *, actor: str, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    if title == incident.title:
        return
    db.add(_event(incident, "comment", actor, {"text": f"Title changed to: {title}",
                                                "title_from": incident.title}, now))  # fmt: skip
    incident.title = title
    incident.updated_at = now


async def comment(
    db: AsyncSession, incident: Incident, text: str, *, actor: str, now: datetime | None = None
) -> IncidentEvent:
    now = now or datetime.now(UTC)
    event = _event(incident, "comment", actor, {"text": text[:MAX_COMMENT]}, now)
    db.add(event)
    incident.updated_at = now
    await db.flush()
    return event


async def mitigate_quiet(
    db: AsyncSession, *, quiet: timedelta = timedelta(minutes=5), now: datetime | None = None
) -> list[str]:
    """Open or investigating incidents whose signals have all been resolved for ``quiet``
    become mitigated. The wait keeps a signal that resolves and fires again (a spike
    hovering at its threshold, an alert re-evaluated) from flipping the status."""
    now = now or datetime.now(UTC)
    firing = (
        select(func.count())
        .select_from(IncidentSignal)
        .join(Signal, Signal.id == IncidentSignal.signal_id)
        .where(IncidentSignal.incident_id == Incident.id, Signal.status == SignalStatus.FIRING)
        .correlate(Incident)
        .scalar_subquery()
    )
    last_end = (
        select(func.max(Signal.ends_at))
        .select_from(IncidentSignal)
        .join(Signal, Signal.id == IncidentSignal.signal_id)
        .where(IncidentSignal.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    rows = await db.scalars(
        select(Incident).where(
            Incident.status.in_([IncidentStatus.OPEN, IncidentStatus.INVESTIGATING]),
            firing == 0,
            func.coalesce(last_end, Incident.last_signal_at) <= now - quiet,
        )
    )
    changed = []
    for incident in rows.all():
        await transition(db, incident, IncidentStatus.MITIGATED, actor=SYSTEM, now=now,
                         note="every signal has resolved")  # fmt: skip
        changed.append(incident.ref)
    return changed
